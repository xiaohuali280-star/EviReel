"""E2 acceptance findings (second review) - regression tests on REAL PostgreSQL + S3 + media, with the
real stage functions and runner (broker replaced by e2_support.drain_inline; models are test doubles).

1. visible publication is fenced by job sequence (stale publish / stale transcript head cannot roll back)
2. an audio-only failure does not stop the visual branch
3. the recovery scan re-checks under lock (renewal / takeover after the candidate read)
4. notes batches and exports run as pipeline jobs; auto notes after publish
5. orphaned pipeline objects are swept after a grace period; referenced products are kept
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from e1_support import requires_infra
from e2_factory import calls
from e2_support import attempt_rows, drain_inline, job_row, stage_rows
from test_e1_platform import As, infra  # noqa: F401  (fixtures)
from test_e2_pipeline import accepted, media, worker_app  # noqa: F401  (fixtures)

pytestmark = requires_infra


# ---------------------------------------------------------------------------------------- 1. fencing
def test_stale_publish_of_an_older_job_cannot_roll_back_a_newer_publication(worker_app, media):
    settings, app, make_ctx = worker_app()
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        ctx = make_ctx()
        first = view["job_id"]
        assert drain_inline(ctx, first)["status"] == "succeeded"
        first_gen = alice.get(f"/api/videos/{view['video_id']}").json()["active_generation"]
        second = alice.post(f"/api/videos/{view['video_id']}/index").json()["job_id"]
        assert drain_inline(ctx, second)["status"] == "succeeded"
        second_gen = alice.get(f"/api/videos/{view['video_id']}").json()["active_generation"]
        assert second_gen != first_gen
        # Old executors of job 1 resume now, after job 2 published: publish only decides (writes nothing
        # visible, and its ledger commit is rejected); a stale projection of job 1 is fenced out.
        from vam.pipeline.stages import project, publish
        old = publish(ctx, {"job": job_row(ctx.db, first), "stage": stage_rows(ctx.db, first)["publish"]})
        assert ctx.repo.complete(first, "publish", "f" * 32, old.output) is False
        assert alice.get(f"/api/videos/{view['video_id']}").json()["active_generation"] == second_gen
        result = project(ctx, {"job": job_row(ctx.db, first), "stage": stage_rows(ctx.db, first)["project"]})
        assert result.output["published"] is False
        assert result.output["transcript_heads_moved"] is False
        assert alice.get(f"/api/videos/{view['video_id']}").json()["active_generation"] == second_gen
        # A re-run of the CURRENT job's projection is idempotent (PG/SQLite window recovery).
        again = project(ctx, {"job": job_row(ctx.db, second), "stage": stage_rows(ctx.db, second)["project"]})
        assert again.output["published"] is True
        assert alice.get(f"/api/videos/{view['video_id']}").json()["active_generation"] == second_gen


def test_transcript_heads_only_move_forward_in_job_order(tmp_path):
    from vam.memory.transcript import TranscriptStore
    store = TranscriptStore(tmp_path / "t.sqlite3", "v_" + "1" * 32)
    for version in ("a" * 64, "b" * 64):
        store.stage(version=version, transcription_key=version, source_sha256="s", asr_fingerprint="{}",
                    chunk_config="{}", embedding_identity="{}", segments=[], chunks=[], language="zh", metadata={})
        store.complete_version(version, empty=True)
    assert store.publish_head("b" * 64, fence=2) is True        # newer job published
    assert store.publish_head("a" * 64, fence=1) is False       # stale older job: rejected
    assert store.state()["text_version"] == "b" * 64
    assert store.publish_head("b" * 64, fence=2) is True        # same job again: idempotent


# ---------------------------------------------------------------------------------------- 2. audio isolation
def test_audio_failure_does_not_stop_the_visual_branch(worker_app, media, monkeypatch):
    settings, app, make_ctx = worker_app(pipeline_stage_max_attempts=2)
    from vam.asr.audio import ASRError
    import vam.pipeline.stages as stages

    def broken_audio(*args, **kwargs):
        raise ASRError("audio_timeout", "injected: audio extraction timed out")
    monkeypatch.setattr(stages, "extract_audio", broken_audio)
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        job = drain_inline(make_ctx(), view["job_id"])
        rows = stage_rows(app.state.pipeline.repo.db, view["job_id"])
        assert {s: r["status"] for s, r in rows.items()} == {
            "prepare": "succeeded", "audio": "failed", "asr": "skipped", "transcript_index": "skipped",
            "vision": "succeeded", "publish": "succeeded", "project": "succeeded"}
        assert rows["audio"]["attempts"] == 2 and rows["vision"]["attempts"] == 1
        assert job["status"] == "partial" and job["error_code"] == "audio_timeout"
        detail = alice.get(f"/api/videos/{view['video_id']}").json()
        assert detail["capabilities"]["vision"] is True and detail["capabilities"]["playback"] is True
        assert detail["transcript"]["status"] == "failed"


# ---------------------------------------------------------------------------------------- 3. scan race
def _expire(repo, job_id, stage):
    with repo.db.transaction() as conn:
        conn.execute("UPDATE job_stages SET lease_until=now() - interval '1 second', "
                     "last_dispatch_at=now() - interval '1 hour' WHERE job_id=%s AND stage=%s", (job_id, stage))


def test_scan_does_not_fail_an_attempt_renewed_after_the_candidate_read(worker_app, media):
    settings, app, _ = worker_app(pipeline_stage_max_attempts=1)
    with TestClient(app) as client:
        _, view = accepted(client, app, media["red"])
        repo, job_id = app.state.pipeline.repo, view["job_id"]
        token = repo.claim(job_id, "prepare", "w")["stage"]["attempt_token"]
        _expire(repo, job_id, "prepare")
        renewed = []
        summary = repo.scan(_after_select=lambda expired, stale: renewed.append(repo.heartbeat(job_id, "prepare", token)))
        assert renewed == [True]
        assert summary["exhausted"] == 0 and summary["lease_expired"] == 0
        row = stage_rows(repo.db, job_id)["prepare"]
        assert row["status"] == "running" and row["attempt_token"] == token
        assert repo.complete(job_id, "prepare", token, {}) is True        # the healthy attempt finishes


def test_scan_does_not_act_on_an_attempt_taken_over_after_the_candidate_read(worker_app, media):
    settings, app, _ = worker_app(pipeline_stage_max_attempts=3)
    with TestClient(app) as client:
        _, view = accepted(client, app, media["red"])
        repo, job_id = app.state.pipeline.repo, view["job_id"]
        repo.claim(job_id, "prepare", "w-old")
        _expire(repo, job_id, "prepare")
        takeover = []
        summary = repo.scan(_after_select=lambda e, s: takeover.append(repo.claim(job_id, "prepare", "w-new")))
        new_token = takeover[0]["stage"]["attempt_token"]
        assert summary["lease_expired"] == 0 and summary["exhausted"] == 0
        row = stage_rows(repo.db, job_id)["prepare"]
        assert row["status"] == "running" and row["attempt_token"] == new_token
        with repo.db.connect() as conn:   # no extra dispatch for the healthy new attempt
            assert conn.execute("SELECT COUNT(*) AS n FROM outbox_events WHERE stage='prepare'").fetchone()["n"] == 1


# ---------------------------------------------------------------------------------------- 4. notes & export
def test_auto_notes_follow_publication_and_run_as_checkpointed_pipeline_jobs(worker_app, media, monkeypatch):
    settings, app, make_ctx = worker_app(study_auto_generate=True)
    monkeypatch.setenv("E2_NOTES", "fail_first")     # first model output invalid -> retried stage
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        ctx = make_ctx()
        assert drain_inline(ctx, view["job_id"])["status"] == "succeeded"
        repo = app.state.pipeline.repo
        with repo.db.connect() as conn:
            notes = conn.execute("SELECT * FROM jobs WHERE kind='notes'").fetchone()
        assert notes["request_id"] == f"auto-notes:{view['job_id']}"   # created in the publish transaction
        assert drain_inline(ctx, notes["job_id"])["status"] == "succeeded"
        assert [a["outcome"] for a in attempt_rows(repo.db, notes["job_id"], "notes_draft")] == ["retry", "succeeded"]
        state = alice.get(f"/api/videos/{view['video_id']}/study").json()
        assert state["job"]["job_id"] == notes["job_id"] and state["job"]["status"] == "succeeded"
        artifact = state["artifact"]
        assert artifact["artifact_id"] == "a_" + notes["job_id"] and artifact["content"]["chapters"]
        model_calls = calls(worker_app.counters, "notes")
        # A user-requested notes job for the same content reuses every verified batch (no model call).
        manual = alice.post(f"/api/videos/{view['video_id']}/study", json={"request_id": "r_" + uuid.uuid4().hex}).json()
        assert drain_inline(ctx, manual["job_id"])["status"] == "succeeded"
        assert stage_rows(repo.db, manual["job_id"])["notes_draft"]["output"]["batches"] == {"reused": 1, "computed": 0}
        assert calls(worker_app.counters, "notes") == model_calls
        # Duplicate execution of the publication cannot duplicate the artifact.
        from vam.pipeline.stages import notes_publish
        notes_publish(ctx, {"job": job_row(repo.db, manual["job_id"]),
                            "stage": stage_rows(repo.db, manual["job_id"])["notes_publish"]})
        with app.state.indexing.registry.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM study_artifacts WHERE job_id=?", (manual["job_id"],)).fetchone()[0] == 1
            latest = conn.execute("SELECT job_id FROM study_artifacts ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        assert latest == manual["job_id"]                     # ordered by job creation, not by write time


def test_notes_jobs_survive_api_restart_and_exports_are_pipeline_jobs(worker_app, media):
    settings, app, make_ctx = worker_app()
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        ctx = make_ctx()
        assert drain_inline(ctx, view["job_id"])["status"] == "succeeded"
        request_id = "r_" + uuid.uuid4().hex
        submitted = alice.post(f"/api/videos/{view['video_id']}/study", json={"request_id": request_id}).json()
        assert alice.post(f"/api/videos/{view['video_id']}/study", json={"request_id": request_id}).json()["job_id"] == submitted["job_id"]
    token = alice.headers["Authorization"][7:]
    _, app2, _ = worker_app()
    with TestClient(app2) as client:   # API restart: the notes job is not marked interrupted
        with app2.state.indexing.registry.db.connect() as conn:
            assert conn.execute("SELECT status FROM study_jobs WHERE job_id=?", (submitted["job_id"],)).fetchone()[0] == "queued"
        assert drain_inline(ctx, submitted["job_id"])["status"] == "succeeded"
        user = As(client, token)
        artifact_id = "a_" + submitted["job_id"]
        made = user.post(f"/api/videos/{view['video_id']}/study/artifacts/{artifact_id}/exports?format=md").json()
        again = user.post(f"/api/videos/{view['video_id']}/study/artifacts/{artifact_id}/exports?format=md").json()
        assert made["job_id"] == again["job_id"]
        assert drain_inline(ctx, made["job_id"])["status"] == "succeeded"
        download = user.get(f"/api/videos/{view['video_id']}/study/exports/{made['job_id']}/download")
        direct = user.get(f"/api/videos/{view['video_id']}/study/artifacts/{artifact_id}/export?format=md")
        assert download.status_code == 200 and download.content == direct.content
        assert "attachment" in download.headers["content-disposition"]


# ---------------------------------------------------------------------------------------- 5. orphan sweep
def test_orphaned_pipeline_objects_are_swept_and_referenced_products_kept(worker_app, media, tmp_path):
    settings, app, make_ctx = worker_app()
    with TestClient(app) as client:
        _, view = accepted(client, app, media["red"])
        ctx = make_ctx()
        assert drain_inline(ctx, view["job_id"])["status"] == "succeeded"
        repo, store = ctx.repo, ctx.store
        job = job_row(ctx.db, view["job_id"])
        scratch = tmp_path / "orphan.json"
        # (a) crash between upload and checkpoint row: an object nothing references
        orphan = ctx.put_json(ctx.artifact_key(job, "asr", "c" * 32, "segments.json"), {"x": 1}, scratch)["key"]
        # (b) an attempt that is still running: protected even though unreferenced
        live_token = "d" * 32
        with repo.db.transaction() as conn:
            conn.execute("UPDATE job_stages SET status='running', attempt_token=%s, lease_until=now() + interval '1 hour', "
                         "finished_at=NULL WHERE job_id=%s AND stage='publish'", (live_token, view["job_id"]))
        live = ctx.put_json(ctx.artifact_key(job, "publish", live_token, "x.json"), {"x": 2}, scratch)["key"]
        referenced = stage_rows(ctx.db, view["job_id"])["audio"]["output"]["audio"]["key"]
        with repo.db.connect() as conn:
            checkpointed = [r["object_key"] for r in conn.execute("SELECT object_key FROM checkpoints")]
        from vam.pipeline.sweep import sweep_orphans
        prefix = "pipeline/"
        assert sweep_orphans(ctx.db, store, prefix, grace_s=3600)["orphans"] == 0     # grace protects fresh objects
        result = sweep_orphans(ctx.db, store, prefix, grace_s=0, now=time.time() + 5)
        assert result["orphans"] == 1
        keys = {k for k, _, _ in store.list_objects(prefix)}
        assert orphan not in keys
        assert {live, referenced, *checkpointed} <= keys
        with ctx.db.connect() as conn:
            row = conn.execute("SELECT reason, state FROM object_deletions WHERE object_key=%s", (orphan,)).fetchone()
        assert (row["reason"], row["state"]) == ("pipeline_orphan", "done")
