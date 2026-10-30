"""E2 third review - notes publication path, on REAL PostgreSQL + S3 + media with the real stages/runner/API.

1. a stale notes_publish attempt resuming after its takeover failed cannot make the notes visible/succeeded
2. auto notes are accepted only after the video projection, so a notes worker can never run first
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from e1_support import requires_infra
from e2_support import drain_inline, job_row, stage_rows
from test_e1_platform import infra  # noqa: F401  (fixture)
from test_e2_pipeline import accepted, media, worker_app  # noqa: F401  (fixtures)
from test_e2_rereview import run_until

pytestmark = requires_infra


def test_stale_notes_publish_cannot_publish_after_the_takeover_failed(worker_app, media):
    settings, app, make_ctx = worker_app()
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        ctx, repo = make_ctx(), app.state.pipeline.repo
        assert drain_inline(ctx, view["job_id"])["status"] == "succeeded"
        notes = alice.post(f"/api/videos/{view['video_id']}/study", json={"request_id": "r_" + uuid.uuid4().hex}).json()
        job_id = notes["job_id"]
        run_until(ctx, job_id, "notes_publish")                 # draft done
        old = repo.claim(job_id, "notes_publish", "w-old")      # pauses
        with repo.db.transaction() as conn:
            conn.execute("UPDATE job_stages SET lease_until=now() - interval '1 second' WHERE job_id=%s "
                         "AND stage='notes_publish'", (job_id,))
        new = repo.claim(job_id, "notes_publish", "w-new")
        assert repo.fail(job_id, "notes_publish", new["stage"]["attempt_token"], code="publish_rejected",
                         message="injected permanent failure", transient=False) == "failed"
        assert drain_inline(ctx, job_id)["status"] == "failed"  # project applies the failed outcome
        # The stale attempt resumes: it only decides (writes nothing) and its commit is refused.
        from vam.pipeline.stages import notes_publish, project
        stale = notes_publish(ctx, old)
        assert repo.complete(job_id, "notes_publish", old["stage"]["attempt_token"], stale.output) is False
        # Even a re-run of the projection derives the failed state from the ledger.
        project(ctx, {"job": job_row(ctx.db, job_id), "stage": stage_rows(ctx.db, job_id)["project"]})
        with app.state.indexing.registry.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM study_artifacts WHERE job_id=?", (job_id,)).fetchone()[0] == 0
        state = alice.get(f"/api/videos/{view['video_id']}/study").json()
        assert state["job"]["status"] == "failed" and state["artifact"] is None
        job = alice.get(f"/api/videos/{view['video_id']}/study/jobs/{job_id}").json()
        assert (job["status"], job["artifact_id"]) == ("failed", None)
        assert alice.get(f"/api/videos/{view['video_id']}/study/artifacts/a_{job_id}/export?format=md").status_code == 404


def test_auto_notes_are_accepted_only_after_the_video_projection(worker_app, media):
    settings, app, make_ctx = worker_app(study_auto_generate=True)
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        ctx, repo = make_ctx(), app.state.pipeline.repo
        run_until(ctx, view["job_id"], "project")              # publish decided; projection still pending

        def notes_jobs():
            with repo.db.connect() as conn:
                return conn.execute("SELECT * FROM jobs WHERE kind='notes'").fetchall()
        assert job_row(repo.db, view["job_id"])["status"] == "succeeded"
        assert notes_jobs() == []                               # nothing a notes worker could run yet
        assert drain_inline(ctx, view["job_id"])["status"] == "succeeded"   # project commits + accepts notes
        [notes] = notes_jobs()
        assert notes["request_id"] == f"auto-notes:{view['job_id']}"
        assert drain_inline(ctx, notes["job_id"])["status"] == "succeeded"
        assert stage_rows(repo.db, notes["job_id"])["notes_draft"]["status"] == "succeeded"
        state = alice.get(f"/api/videos/{view['video_id']}/study").json()
        assert state["artifact"]["artifact_id"] == "a_" + notes["job_id"] and state["artifact"]["content"]["chapters"]
