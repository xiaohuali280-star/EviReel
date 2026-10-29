"""E2 re-review findings - regression tests on REAL PostgreSQL + S3 + media with the real stages/runner.

1. cleanup vs. a new checkpoint reference to the same object (per-key lock; queued keys refused)
2. manual retry of an export keeps its business parameters
3. a stale attempt of the SAME job cannot override the ledger's failed outcome (decide/project split)
4. a notes job ended by the scanner shows its terminal state (projection + read-time ledger overlay)
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from e1_support import requires_infra
from e2_support import drain_inline, job_row, stage_rows
from test_e1_platform import infra  # noqa: F401  (fixture)
from test_e2_pipeline import accepted, media, worker_app  # noqa: F401  (fixtures)

pytestmark = requires_infra


def run_until(ctx, job_id, stage, timeout=120):
    """Execute dispatched stages until `stage` becomes runnable; leave it unexecuted."""
    from vam.pipeline.runner import execute
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stage_rows(ctx.db, job_id)[stage]["status"] == "pending":
            return
        sent = []
        ctx.repo.publish_due(sent.append)
        for row in sent:
            if row["stage"] != stage:
                execute(ctx, row["job_id"], row["stage"])
        time.sleep(0.05)
    raise AssertionError(f"{stage} never became runnable")


# ---------------------------------------------------------------------------------------- 1. GC protocol
class FlakyDelete:
    def __init__(self, store):
        self.store, self.down = store, True

    def delete(self, key):
        from vam.platform.object_store import ObjectStoreError
        if self.down:
            raise ObjectStoreError("injected: delete unavailable")
        return self.store.delete(key)

    def __getattr__(self, name):
        return getattr(self.store, name)


def test_queued_object_cannot_gain_a_checkpoint_and_referenced_object_is_never_deleted(worker_app, media, tmp_path):
    settings, app, make_ctx = worker_app()
    with TestClient(app) as client:
        _, view = accepted(client, app, media["red"])
        ctx = make_ctx()
        repo, job = ctx.repo, job_row(ctx.db, view["job_id"])
        flaky = FlakyDelete(ctx.store)
        from vam.pipeline.sweep import sweep_orphans
        from vam.platform.cleanup import ObjectCleaner, enqueue
        # A paused attempt's verified upload, judged orphan after the grace period; the delete fails.
        ref = ctx.put_json(ctx.artifact_key(job, "asr", "e" * 32, "segments.json"), {"x": 1}, tmp_path / "a.json")
        assert sweep_orphans(ctx.db, flaky, "pipeline/", grace_s=0, now=time.time() + 5)["orphans"] == 1
        with ctx.db.connect() as conn:
            assert conn.execute("SELECT state FROM object_deletions WHERE object_key=%s", (ref["key"],)).fetchone()["state"] == "pending"
        # The paused attempt resumes and tries to register it: refused (the review's failure scenario).
        reuse = "asr:" + "1" * 64
        assert repo.save_checkpoint(reuse, ref["key"], ref["sha256"], ref["size"], "e" * 32) is None
        assert repo.checkpoint(reuse) is None
        flaky.down = False
        assert ObjectCleaner(ctx.db, flaky).drain() == 0           # still inside the retry backoff
        assert ObjectCleaner(ctx.db, flaky).delete_now(ref["key"]) is True
        assert ref["key"] not in {k for k, _, _ in ctx.store.list_objects("pipeline/")}
        # Opposite order: registered first -> the sweep never queues it.
        ref2 = ctx.put_json(ctx.artifact_key(job, "asr", "f" * 32, "segments.json"), {"x": 2}, tmp_path / "b.json")
        assert repo.save_checkpoint("asr:" + "2" * 64, ref2["key"], ref2["sha256"], ref2["size"], "f" * 32)
        assert sweep_orphans(ctx.db, ctx.store, "pipeline/", grace_s=0, now=time.time() + 5)["orphans"] == 0
        # Even a queued key that is referenced by the time of deletion is kept (re-check under the lock).
        with ctx.db.transaction() as conn:
            enqueue(conn, ref2["key"], "pipeline_orphan")
        assert ObjectCleaner(ctx.db, ctx.store).delete_now(ref2["key"]) is False
        with ctx.db.connect() as conn:
            row = conn.execute("SELECT state, last_error FROM object_deletions WHERE object_key=%s", (ref2["key"],)).fetchone()
        assert (row["state"], row["last_error"]) == ("skipped", "still_referenced:checkpoint")
        assert ref2["key"] in {k for k, _, _ in ctx.store.list_objects("pipeline/")}


# ---------------------------------------------------------------------------------------- 2. export retry
def test_failed_export_retried_by_hand_keeps_artifact_and_format(worker_app, media, monkeypatch):
    settings, app, make_ctx = worker_app()
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        ctx = make_ctx()
        assert drain_inline(ctx, view["job_id"])["status"] == "succeeded"
        notes = alice.post(f"/api/videos/{view['video_id']}/study", json={"request_id": "r_" + uuid.uuid4().hex}).json()
        assert drain_inline(ctx, notes["job_id"])["status"] == "succeeded"
        artifact_id = "a_" + notes["job_id"]
        import vam.study_exports as exports
        from vam.video_registry import DomainError

        def broken(*args, **kwargs):
            raise DomainError(422, "export_too_large", "injected permanent export failure")
        real = exports.export_study
        monkeypatch.setattr(exports, "export_study", broken)
        made = alice.post(f"/api/videos/{view['video_id']}/study/artifacts/{artifact_id}/exports?format=pdf").json()
        assert drain_inline(ctx, made["job_id"])["status"] == "failed"
        monkeypatch.setattr(exports, "export_study", real)
        retry = alice.post(f"/api/jobs/{made['job_id']}/retry").json()["job_id"]
        assert alice.post(f"/api/jobs/{made['job_id']}/retry").json()["job_id"] == retry      # idempotent
        assert app.state.pipeline.repo.job(retry)["params"] == {"kind": "export", "video_id": view["video_id"],
                                                                "artifact_id": artifact_id, "format": "pdf"}
        assert drain_inline(ctx, retry)["status"] == "succeeded"
        download = alice.get(f"/api/videos/{view['video_id']}/study/exports/{retry}/download")
        assert download.status_code == 200 and download.content.startswith(b"%PDF")


# ---------------------------------------------------------------------------------------- 3. same-job stale attempt
def test_stale_attempt_of_the_same_job_cannot_override_its_failed_outcome(worker_app, media):
    settings, app, make_ctx = worker_app()
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        ctx, repo, job_id = make_ctx(), app.state.pipeline.repo, view["job_id"]
        run_until(ctx, job_id, "publish")
        old = repo.claim(job_id, "publish", "w-old")          # pauses (the stale attempt)
        with repo.db.transaction() as conn:
            conn.execute("UPDATE job_stages SET lease_until=now() - interval '1 second' WHERE job_id=%s AND stage='publish'",
                         (job_id,))
        new = repo.claim(job_id, "publish", "w-new")          # takeover, which fails permanently
        assert repo.fail(job_id, "publish", new["stage"]["attempt_token"], code="publish_rejected",
                         message="injected permanent failure", transient=False) == "failed"
        assert drain_inline(ctx, job_id)["status"] == "failed"   # project applies the ledger's outcome
        visible = alice.get(f"/api/videos/{view['video_id']}").json()
        assert visible["status"] == "failed" and visible["active_generation"] is None
        # The stale attempt resumes: deciding writes nothing visible, and its commit is refused.
        from vam.pipeline.stages import project, publish
        stale = publish(ctx, old)
        assert repo.complete(job_id, "publish", old["stage"]["attempt_token"], stale.output,
                             job_result=stale.job_result) is False
        # Any re-run of the projection derives the same failed state from the ledger.
        project(ctx, {"job": job_row(ctx.db, job_id), "stage": stage_rows(ctx.db, job_id)["project"]})
        after = alice.get(f"/api/videos/{view['video_id']}").json()
        assert (after["status"], after["active_generation"]) == ("failed", None)
        assert job_row(ctx.db, job_id)["status"] == "failed"
        assert alice.get(f"/api/jobs/{job_id}").json()["status"] == "failed"


# ---------------------------------------------------------------------------------------- 4. notes ended by scan
def test_notes_job_ended_by_the_scanner_shows_its_terminal_state(worker_app, media):
    settings, app, make_ctx = worker_app(pipeline_stage_max_attempts=1)
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        ctx, repo = make_ctx(), app.state.pipeline.repo
        assert drain_inline(ctx, view["job_id"])["status"] == "succeeded"
        notes = alice.post(f"/api/videos/{view['video_id']}/study", json={"request_id": "r_" + uuid.uuid4().hex}).json()
        claim = repo.claim(notes["job_id"], "notes_draft", "w")    # last attempt; the worker then dies hard
        ctx.project(claim["job"], status="running", phase="generating")
        with repo.db.transaction() as conn:
            conn.execute("UPDATE job_stages SET lease_until=now() - interval '1 second', "
                         "last_dispatch_at=now() - interval '1 hour' WHERE job_id=%s", (notes["job_id"],))
        assert repo.scan()["exhausted"] == 1
        url = f"/api/videos/{view['video_id']}/study/jobs/{notes['job_id']}"
        # Read-time overlay: the ledger's terminal state wins over the stale mirror immediately...
        early = alice.get(url).json()
        assert (early["status"], early["phase"], early["error_code"]) == ("failed", "failed", "attempts_exhausted")
        # ...and the final project stage writes it into the read model.
        assert drain_inline(ctx, notes["job_id"])["status"] == "failed"
        stages = stage_rows(repo.db, notes["job_id"])
        assert (stages["notes_publish"]["status"], stages["project"]["status"]) == ("skipped", "succeeded")
        with app.state.indexing.registry.db.connect() as conn:
            row = conn.execute("SELECT status, error_code FROM study_jobs WHERE job_id=?", (notes["job_id"],)).fetchone()
        assert tuple(row) == ("failed", "attempts_exhausted")
        assert alice.get(f"/api/videos/{view['video_id']}/study").json()["job"]["status"] == "failed"
