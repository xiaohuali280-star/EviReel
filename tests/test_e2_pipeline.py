"""E2 ledger protocol and stage behaviour on REAL PostgreSQL + REAL S3 + real media (ffmpeg).

RabbitMQ is not involved here: outbox rows are handed to the real runner in-process
(e2_support.drain_inline). Broker delivery, worker processes and kill/restart faults are in
tests/test_e2_faults.py. Models are the e2_factory test double.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from e1_support import requires_infra
from e2_factory import calls
from e2_support import attempt_rows, drain_inline, e2_values, job_row, make_media, stage_rows, upload_video
from test_e1_platform import infra, users  # noqa: F401  (fixtures)

pytestmark = requires_infra


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2media")
    return {"red": make_media(root / "red.mp4", color="red").read_bytes()}


@pytest.fixture
def worker_app(tmp_path, infra, monkeypatch):  # noqa: F811
    import vam.config as config
    from vam.config import Settings
    from vam.pipeline.context import WorkerContext
    from vam.server.app import create_app
    counters = tmp_path / "counters"
    counters.mkdir()
    monkeypatch.setenv("E2_COUNTER_DIR", str(counters))

    def start(**overrides):
        settings = Settings(**e2_values(tmp_path, infra, **overrides))
        monkeypatch.setattr(config, "_settings", settings)
        app = create_app(settings=settings)
        return settings, app, lambda: WorkerContext(settings)
    start.counters = counters
    return start


def accepted(client, app, data):
    alice, _ = users(client, app, "alice")["alice"]
    view = upload_video(client, alice, data)
    return alice, view


# ---------------------------------------------------------------------------- acceptance
def test_upload_is_accepted_durably_without_running_anything_in_the_api(worker_app, media):
    settings, app, _ = worker_app()
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        job = job_row(app.state.pipeline.repo.db, view["job_id"])
        assert job["status"] == "queued" and job["request_id"] == f"upload:{view['upload_id']}"
        stages = stage_rows(app.state.pipeline.repo.db, view["job_id"])
        assert {s: r["status"] for s, r in stages.items()} == {
            "prepare": "pending", "audio": "waiting", "asr": "waiting", "transcript_index": "waiting",
            "vision": "waiting", "publish": "waiting", "project": "waiting"}
        with app.state.pipeline.repo.db.connect() as conn:
            outbox = conn.execute("SELECT dedupe_key, queue, status FROM outbox_events").fetchall()
        assert outbox == [{"dedupe_key": f"{view['job_id']}:prepare:1", "queue": "media", "status": "pending"}]
        # The read model mirrors the job for the existing UI; nothing ran in-process.
        detail = alice.get(f"/api/jobs/{view['job_id']}").json()
        assert detail["status"] == "queued" and [s["stage"] for s in detail["pipeline"]["stages"]]
        assert not app.state.indexing.tasks


def test_request_id_idempotency_conflict_and_one_active_job(worker_app, media):
    settings, app, _ = worker_app()
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        repo = app.state.pipeline.repo
        video_id = view["video_id"]
        # One unfinished job per video, whoever asks.
        assert alice.post(f"/api/videos/{video_id}/index").json()["error"]["code"] == "index_running"
        with repo.db.transaction() as conn:   # finish the accepted job so new ones may start
            conn.execute("UPDATE jobs SET status='failed', finished_at=now() WHERE job_id=%s", (view["job_id"],))
        headers = {"Idempotency-Key": "reindex-0001"}
        first = alice.post(f"/api/videos/{video_id}/index", headers=headers)
        again = alice.post(f"/api/videos/{video_id}/index", headers=headers)
        assert first.status_code == 202 and again.json()["job_id"] == first.json()["job_id"]
        # Same key, different parameters (transcript-only job): conflict, nothing created.
        clash = alice.post(f"/api/videos/{video_id}/transcripts/index", headers=headers)
        assert clash.status_code == 409 and clash.json()["error"]["code"] == "request_conflict"
        with repo.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 2
        # Concurrent duplicate submissions of a new key resolve to one job.
        with repo.db.transaction() as conn:
            conn.execute("UPDATE jobs SET status='failed', finished_at=now() WHERE status IN ('queued','running')")
        with ThreadPoolExecutor(6) as pool:
            ids = set(pool.map(lambda _: repo.submit(video_id=video_id, kind="video", request_id="http:race-0001")[0]["job_id"],
                               range(6)))
        assert len(ids) == 1


# ---------------------------------------------------------------------------- leases & tokens
def test_claim_is_exclusive_and_a_stale_attempt_cannot_commit(worker_app, media):
    settings, app, make_ctx = worker_app()
    with TestClient(app) as client:
        _, view = accepted(client, app, media["red"])
        repo, job_id = app.state.pipeline.repo, view["job_id"]
        with ThreadPoolExecutor(8) as pool:
            claims = [c for c in pool.map(lambda i: repo.claim(job_id, "prepare", f"w{i}"), range(8)) if c]
        assert len(claims) == 1                      # concurrent consumption: one winner
        old = claims[0]["stage"]["attempt_token"]
        assert repo.claim(job_id, "prepare", "late") is None   # live lease: duplicate is a no-op
        with repo.db.transaction() as conn:          # the first worker hangs past its lease
            conn.execute("UPDATE job_stages SET lease_until=now() - interval '1 second' WHERE job_id=%s", (job_id,))
        takeover = repo.claim(job_id, "prepare", "w-new")
        assert takeover and takeover["stage"]["attempt_token"] != old
        assert repo.heartbeat(job_id, "prepare", old) is False
        assert repo.complete(job_id, "prepare", old, {"stale": True}) is False      # late write rejected
        assert repo.fail(job_id, "prepare", old, code="x", message="x", transient=True) == "lost"
        assert repo.complete(job_id, "prepare", takeover["stage"]["attempt_token"], {"ok": True}) is True
        outcomes = {a["attempt_token"]: a["outcome"] for a in attempt_rows(repo.db, job_id, "prepare")}
        assert outcomes == {old: "lease_lost", takeover["stage"]["attempt_token"]: "succeeded"}
        assert stage_rows(repo.db, job_id)["prepare"]["output"] == {"ok": True}


def test_parallel_branch_completion_activates_the_join_exactly_once(worker_app, media):
    settings, app, _ = worker_app()
    with TestClient(app) as client:
        _, view = accepted(client, app, media["red"])
        repo, job_id = app.state.pipeline.repo, view["job_id"]
        for stage in ("prepare", "audio", "asr"):
            token = repo.claim(job_id, stage, "w")["stage"]["attempt_token"]
            assert repo.complete(job_id, stage, token, {})
        tokens = {s: repo.claim(job_id, s, f"w-{s}")["stage"]["attempt_token"] for s in ("transcript_index", "vision")}
        barrier = threading.Barrier(2)

        def finish(stage):
            barrier.wait()
            return repo.complete(job_id, stage, tokens[stage], {"stage": stage})
        with ThreadPoolExecutor(2) as pool:
            assert all(pool.map(finish, tokens))
        assert stage_rows(repo.db, job_id)["publish"]["status"] == "pending"
        with repo.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM outbox_events WHERE stage='publish'").fetchone()["n"] == 1


def test_transient_failures_back_off_then_exhaust_and_skip_dependents(worker_app, media):
    settings, app, _ = worker_app(pipeline_stage_max_attempts=2, pipeline_retry_base_s=30, pipeline_retry_cap_s=60)
    with TestClient(app) as client:
        _, view = accepted(client, app, media["red"])
        repo, job_id = app.state.pipeline.repo, view["job_id"]
        for stage in ("prepare", "audio"):
            token = repo.claim(job_id, stage, "w")["stage"]["attempt_token"]
            repo.complete(job_id, stage, token, {})
        token = repo.claim(job_id, "asr", "w")["stage"]["attempt_token"]
        assert repo.fail(job_id, "asr", token, code="asr_timeout", message="slow", transient=True) == "retry"
        row = stage_rows(repo.db, job_id)["asr"]
        assert row["status"] == "retry_wait"
        assert repo.claim(job_id, "asr", "early") is None          # not before the backoff elapsed
        with repo.db.connect() as conn:
            delayed = conn.execute("SELECT next_attempt_at - now() AS wait FROM outbox_events WHERE stage='asr' "
                                   "ORDER BY event_id DESC LIMIT 1").fetchone()["wait"].total_seconds()
        assert 10 <= delayed <= 31                                   # equal-jitter backoff on 30 s base
        with repo.db.transaction() as conn:
            conn.execute("UPDATE job_stages SET next_attempt_at=now() WHERE job_id=%s AND stage='asr'", (job_id,))
        token = repo.claim(job_id, "asr", "w2")["stage"]["attempt_token"]
        assert repo.fail(job_id, "asr", token, code="asr_timeout", message="slow", transient=True) == "failed"
        stages = stage_rows(repo.db, job_id)
        assert stages["asr"]["status"] == "failed" and stages["transcript_index"]["status"] == "skipped"
        assert stages["publish"]["status"] == "waiting"              # vision branch still pending
        ledger = app.state.pipeline.failures([view_ws(repo, job_id)])
        assert [(f["stage"], f["outcome"]) for f in ledger] == [("asr", "failed"), ("asr", "retry")]


def view_ws(repo, job_id):
    return repo.job(job_id)["workspace_id"]


def test_scanner_redispatches_expired_leases_and_undelivered_stages(worker_app, media):
    settings, app, _ = worker_app(pipeline_lease_s=5, pipeline_redispatch_after_s=1)
    with TestClient(app) as client:
        _, view = accepted(client, app, media["red"])
        repo, job_id = app.state.pipeline.repo, view["job_id"]
        sent = []
        repo.publish_due(sent.append)                    # message "delivered" but never consumed
        assert [r["stage"] for r in sent] == ["prepare"]
        with repo.db.transaction() as conn:
            conn.execute("UPDATE job_stages SET last_dispatch_at=now() - interval '1 minute' WHERE job_id=%s", (job_id,))
        assert repo.scan()["redispatched"] == 1
        token = repo.claim(job_id, "prepare", "w")["stage"]["attempt_token"]   # worker then dies silently
        with repo.db.transaction() as conn:
            conn.execute("UPDATE job_stages SET lease_until=now() - interval '1 second', "
                         "last_dispatch_at=now() - interval '1 minute' WHERE job_id=%s AND stage='prepare'", (job_id,))
        assert repo.scan()["lease_expired"] == 1
        with repo.db.connect() as conn:
            keys = [r["dedupe_key"] for r in conn.execute("SELECT dedupe_key FROM outbox_events ORDER BY event_id")]
        assert keys == [f"{job_id}:prepare:1", f"{job_id}:prepare:2", f"{job_id}:prepare:3"]
        assert repo.claim(job_id, "prepare", "w2")["stage"]["attempt_token"] != token


def test_outbox_survives_broker_errors_and_a_lost_sent_mark(worker_app, media):
    settings, app, _ = worker_app()
    with TestClient(app) as client:
        _, view = accepted(client, app, media["red"])
        repo = app.state.pipeline.repo

        def down(row):
            raise ConnectionError("broker unavailable")
        assert repo.publish_due(down) == {"sent": 0, "failed": 1}
        with repo.db.connect() as conn:
            row = conn.execute("SELECT * FROM outbox_events").fetchone()
        assert row["status"] == "pending" and row["attempts"] == 1 and "broker unavailable" in row["last_error"]
        with repo.db.transaction() as conn:
            conn.execute("UPDATE outbox_events SET next_attempt_at=now()")
        delivered = []

        def confirmed_then_crash(row):      # broker confirmed; process dies before the outbox commit
            delivered.append(row["dedupe_key"])
            raise SystemExit("crash after publish")
        with pytest.raises(SystemExit):
            repo.publish_due(confirmed_then_crash)
        assert repo.publish_due(lambda r: delivered.append(r["dedupe_key"])) == {"sent": 1, "failed": 0}  # re-published
        assert delivered == [row["dedupe_key"], row["dedupe_key"]]           # duplicate delivery...
        token = repo.claim(view["job_id"], "prepare", "w1")
        assert token and repo.claim(view["job_id"], "prepare", "w2") is None  # ...runs once


# ---------------------------------------------------------------------------- stages end to end
def test_pipeline_publishes_both_branches_and_reuses_checkpoints(worker_app, media):
    settings, app, make_ctx = worker_app()
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        ctx = make_ctx()
        job = drain_inline(ctx, view["job_id"])
        assert job["status"] == "succeeded", (job, stage_rows(ctx.db, view["job_id"]))
        stages = stage_rows(ctx.db, view["job_id"])
        assert stages["transcript_index"]["output"]["batches"] == {"reused": 0, "computed": 3}
        assert calls(worker_app.counters, "embed_texts") == 3
        assert stages["vision"]["output"]["generation"] == attempt_rows(ctx.db, view["job_id"], "vision")[0]["attempt_token"]
        detail = alice.get(f"/api/videos/{view['video_id']}").json()
        assert detail["capabilities"] == {"playback": True, "vision": True, "transcript": True, "transcript_search": True}
        assert detail["active_generation"] == stages["vision"]["output"]["generation"]
        assert alice.get(f"/api/videos/{view['video_id']}/content", headers={"Range": "bytes=0-9"}).status_code == 206
        assert alice.get(f"/api/videos/{view['video_id']}/transcripts?kind=segments").json()["items"]
        assert alice.get(f"/api/jobs/{view['job_id']}").json()["status"] == "succeeded"
        assert calls(worker_app.counters, "transcribe") == 1
        # A transcript-only job reuses the verified ASR checkpoint and every embedding batch.
        rerun = alice.post(f"/api/videos/{view['video_id']}/transcripts/index").json()["job_id"]
        assert drain_inline(ctx, rerun)["status"] == "succeeded"
        again = stage_rows(ctx.db, rerun)
        assert again["asr"]["output"]["reused"] is True
        assert calls(worker_app.counters, "transcribe") == 1
        assert again["transcript_index"]["output"]["reused"] is True
        # Artifacts are attempt-keyed objects in storage, verified by sha256 on every read.
        prep = stages["audio"]["output"]["audio"]
        assert f"/{stages['audio']['attempt_token']}/audio.wav" in prep["key"]
        assert ctx.store.head(prep["key"]).size == prep["size"]


def test_embedding_batches_are_checkpointed_across_a_failed_attempt(worker_app, media, monkeypatch):
    settings, app, make_ctx = worker_app()
    monkeypatch.setenv("E2_EMBED_TEXT", "fail_after:2")   # batches 1-2 succeed, then the provider fails
    with TestClient(app) as client:
        _, view = accepted(client, app, media["red"])
        ctx = make_ctx()
        flipped = []

        def recover_provider(row):
            if row["stage"] == "transcript_index" and stage_rows(ctx.db, view["job_id"])["transcript_index"]["attempts"] >= 1:
                monkeypatch.setenv("E2_EMBED_TEXT", "ok")
                flipped.append(True)
        job = drain_inline(ctx, view["job_id"], before_execute=recover_provider)
        assert flipped and job["status"] == "succeeded"
        attempts = attempt_rows(ctx.db, view["job_id"], "transcript_index")
        assert [a["outcome"] for a in attempts] == ["retry", "succeeded"]
        assert attempts[0]["error_code"] == "transcript_embedding_failed"
        # 3 batches total: 2 computed before the outage + 1 failing call, then only the last batch again.
        assert stage_rows(ctx.db, view["job_id"])["transcript_index"]["output"]["batches"] == {"reused": 2, "computed": 1}
        assert calls(worker_app.counters, "embed_texts") == 4


def test_failed_branch_keeps_the_previous_index_and_reports_partial(worker_app, media, monkeypatch):
    settings, app, make_ctx = worker_app(pipeline_stage_max_attempts=1)
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        ctx = make_ctx()
        assert drain_inline(ctx, view["job_id"])["status"] == "succeeded"
        before = alice.get(f"/api/videos/{view['video_id']}").json()
        transcript_before = alice.get(f"/api/videos/{view['video_id']}/transcripts").json()["items"]
        monkeypatch.setenv("E2_VISION", "fail")
        job_id = alice.post(f"/api/videos/{view['video_id']}/index").json()["job_id"]
        job = drain_inline(ctx, job_id)
        assert job["status"] == "partial"
        stages = stage_rows(ctx.db, job_id)
        assert stages["vision"]["status"] == "failed" and stages["transcript_index"]["status"] == "succeeded"
        after = alice.get(f"/api/videos/{view['video_id']}").json()
        assert after["active_generation"] == before["active_generation"]          # old visual index kept
        assert after["capabilities"]["vision"] is True
        assert alice.get(f"/api/videos/{view['video_id']}/transcripts").json()["items"] == transcript_before
        # The failed attempt's generation was removed; the published one is intact.
        failed_token = attempt_rows(ctx.db, job_id, "vision")[0]["attempt_token"]
        assert not ctx.registry.asset(view["video_id"], f"memory/{failed_token}.sqlite3").exists()
        assert ctx.registry.asset(view["video_id"], f"memory/{before['active_generation']}.sqlite3").exists()
        final = alice.get(f"/api/jobs/{job_id}").json()
        assert final["status"] == "partial" and final["result"]["stages"]["vision"]["status"] == "failed"
        # Manual retry: a new job, idempotent per failed job.
        monkeypatch.setenv("E2_VISION", "ok")
        retry = alice.post(f"/api/jobs/{job_id}/retry").json()["job_id"]
        assert alice.post(f"/api/jobs/{job_id}/retry").json()["job_id"] == retry
        assert drain_inline(ctx, retry)["status"] == "succeeded"
        assert alice.get(f"/api/videos/{view['video_id']}").json()["active_generation"] != before["active_generation"]
        failures = alice.get("/api/pipeline/failures").json()["items"]
        assert any(f["job_id"] == job_id and f["stage"] == "vision" for f in failures)


def test_failed_asr_makes_the_job_partial_and_keeps_vision(worker_app, media, monkeypatch):
    settings, app, make_ctx = worker_app(pipeline_stage_max_attempts=2)
    monkeypatch.setenv("E2_ASR", "transient")
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        ctx = make_ctx()
        job = drain_inline(ctx, view["job_id"])
        stages = stage_rows(ctx.db, view["job_id"])
        assert (stages["asr"]["status"], stages["transcript_index"]["status"]) == ("failed", "skipped")
        assert job["status"] == "partial" and job["error_code"] == "asr_timeout"
        detail = alice.get(f"/api/videos/{view['video_id']}").json()
        assert detail["capabilities"]["vision"] is True and detail["capabilities"]["transcript"] is False
        assert detail["transcript"]["status"] == "failed"


def test_jobs_survive_an_api_restart_and_deletion_waits_for_them(worker_app, media):
    settings, app, make_ctx = worker_app()
    with TestClient(app) as client:
        alice, view = accepted(client, app, media["red"])
        assert alice.delete(f"/api/videos/{view['video_id']}").json()["error"]["code"] == "index_running"
    _, app2, _ = worker_app()
    with TestClient(app2) as client:                 # API restart: the job is not marked interrupted
        assert job_row(app2.state.pipeline.repo.db, view["job_id"])["status"] == "queued"
        with app2.state.indexing.registry.db.connect() as conn:
            assert conn.execute("SELECT status FROM index_jobs WHERE job_id=?", (view["job_id"],)).fetchone()[0] == "queued"
        ctx = make_ctx()
        assert drain_inline(ctx, view["job_id"])["status"] == "succeeded"
        stages = stage_rows(ctx.db, view["job_id"])
        assert stages["publish"]["output"]["decision"]["status"] == "succeeded"
        assert stages["project"]["output"] == {"status": "succeeded", "published": True, "transcript_heads_moved": True}
