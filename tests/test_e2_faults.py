"""E2 failure acceptance with REAL RabbitMQ + PostgreSQL + S3 and SEPARATE dispatcher / Celery
worker processes (Linux only; Celery does not support native Windows workers).

Processes are killed exactly like `kill -9` - either from outside (SIGKILL to the whole process
group) or at named points inside the code (VAM_PIPELINE_FAULT, which calls os._exit(137) in the
running process; one-shot via a marker so the replacement process proceeds). Models are the
e2_factory test double; media, broker, database, storage and processes are real.

Environment: EVIREEL_TEST_DATABASE_URL, EVIREEL_TEST_S3_*, EVIREEL_TEST_BROKER_URL;
optional EVIREEL_TEST_BROKER_STOP / EVIREEL_TEST_BROKER_START (shell commands) for the
"broker temporarily unavailable" case.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from e1_support import REAL_INFRA
from e2_factory import calls
from e2_support import attempt_rows, e2_values, env_for, job_row, make_media, stage_rows, upload_video
from test_e1_platform import infra, users  # noqa: F401  (fixtures)

BROKER = os.environ.get("EVIREEL_TEST_BROKER_URL", "")
pytestmark = pytest.mark.skipif(not (REAL_INFRA and BROKER and sys.platform.startswith("linux")),
                                reason="needs Linux + real PostgreSQL/S3 + EVIREEL_TEST_BROKER_URL (RabbitMQ)")
REPO = Path(__file__).resolve().parents[1]


class Stack:
    """API in-process (TestClient); dispatcher and workers as real OS processes."""

    def __init__(self, tmp_path, infra, monkeypatch, **overrides):  # noqa: F811
        import vam.config as config
        from vam.config import Settings
        from vam.server.app import create_app
        self.tmp = tmp_path
        values = dict(broker_url=BROKER, pipeline_lease_s=6, pipeline_redispatch_after_s=5, pipeline_dispatch_interval_s=0.2,
                      pipeline_retry_base_s=0.5, pipeline_retry_cap_s=2)
        self.values = e2_values(tmp_path, infra, **(values | overrides))
        self.settings = Settings(**self.values)
        monkeypatch.setattr(config, "_settings", self.settings)
        self.counters = tmp_path / "counters"
        self.markers = tmp_path / "markers"
        self.logs = tmp_path / "logs"
        for d in (self.counters, self.markers, self.logs):
            d.mkdir(exist_ok=True)
        self.app = create_app(settings=self.settings)
        self.procs = []
        self._purge()

    def _purge(self):
        """Drop leftovers of earlier tests (they would only be no-ops: their jobs are in other databases).
        Retried: right after a broker restart the port is open before the vhost accepts connections."""
        from vam.pipeline.celery_app import make_app
        for attempt in range(30):
            try:
                make_app(self.settings).control.purge()
                return
            except Exception:
                time.sleep(1)
        raise RuntimeError("RabbitMQ did not accept connections for queue purge")

    def env(self, extra=None):
        env = dict(os.environ) | env_for(self.values)
        env |= {"PYTHONPATH": os.pathsep.join([str(REPO / "tests"), str(REPO)]), "E2_COUNTER_DIR": str(self.counters),
                "VAM_PIPELINE_FAULT_MARKER": str(self.markers), "VAM_PIPELINE_FAULT": ""}
        return env | (extra or {})

    def spawn(self, name, args, extra=None):
        log = (self.logs / f"{name}.log").open("ab")
        proc = subprocess.Popen(args, cwd=REPO, env=self.env(extra), stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        proc.name = name
        self.procs.append(proc)
        return proc

    def dispatcher(self, name="dispatcher", **extra):
        return self.spawn(name, [sys.executable, "-m", "vam.pipeline.dispatcher"], extra)

    def worker(self, name="worker", queues="media,asr,embed,control", concurrency=1, **extra):
        return self.spawn(name, [sys.executable, "-m", "celery", "-A", "vam.pipeline.celery_app", "worker",
                                 "-Q", queues, "-c", str(concurrency), "-P", "prefork", "-n", f"{name}@%h",
                                 "--without-gossip", "--without-mingle", "-l", "INFO"], extra)

    @staticmethod
    def kill(proc):
        """kill -9 of the whole process group (worker master + pool children)."""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=30)

    def log(self, name):
        path = self.logs / f"{name}.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def wait(self, predicate, timeout=180, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.3)
        raise AssertionError(f"timed out waiting for {what}\n" + "\n".join(
            f"--- {p.name} ---\n{self.log(p.name)[-4000:]}" for p in self.procs))

    def wait_job(self, job_id, timeout=240):
        db = self.app.state.pipeline.repo.db
        return self.wait(lambda: (row := job_row(db, job_id)) and row["status"] in {"succeeded", "partial", "failed"} and row,
                         timeout, f"job {job_id} -> {stage_rows(db, job_id)}")

    def stage(self, job_id, name):
        return stage_rows(self.app.state.pipeline.repo.db, job_id)[name]

    def attempts(self, job_id, stage=None):
        return attempt_rows(self.app.state.pipeline.repo.db, job_id, stage)

    def close(self):
        for proc in self.procs:
            if proc.poll() is None:
                self.kill(proc)


@pytest.fixture
def stack(tmp_path, infra, monkeypatch):  # noqa: F811
    stacks = []

    def make(**overrides):
        s = Stack(tmp_path, infra, monkeypatch, **overrides)
        stacks.append(s)
        return s
    yield make
    for s in stacks:
        s.close()


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    return make_media(tmp_path_factory.mktemp("e2fault") / "lecture.mp4").read_bytes()


def submit(s, client, video, name="alice"):
    user, _ = users(client, s.app, name)[name]
    return user, upload_video(client, user, video)


# 1. Business job committed, process dies before anything reached the broker.
def test_accepted_job_survives_a_dispatcher_crash_before_publishing(stack, video):
    s = stack()
    with TestClient(s.app) as client:
        _, view = submit(s, client, video)
        crashed = s.dispatcher("dispatcher-1", VAM_PIPELINE_FAULT="dispatcher_before_publish")
        assert crashed.wait(timeout=60) == 137
        with s.app.state.pipeline.repo.db.connect() as conn:
            assert conn.execute("SELECT status FROM outbox_events").fetchone()["status"] == "pending"
        s.worker()
        s.dispatcher("dispatcher-2")
        assert s.wait_job(view["job_id"])["status"] == "succeeded"
        assert all(len(s.attempts(view["job_id"], st)) == 1 for st in ("prepare", "asr", "transcript_index", "vision", "publish"))


# 2. Broker confirmed, outbox not marked sent, dispatcher dies: the message is published twice.
def test_republished_message_after_a_lost_sent_mark_does_not_republish_results(stack, video):
    s = stack()
    with TestClient(s.app) as client:
        _, view = submit(s, client, video)
        s.worker()
        crashed = s.dispatcher("dispatcher-1", VAM_PIPELINE_FAULT="dispatcher_after_publish")
        assert crashed.wait(timeout=60) == 137
        s.dispatcher("dispatcher-2")
        assert s.wait_job(view["job_id"])["status"] == "succeeded"
        # The duplicate was delivered and consumed as a no-op; every stage ran and published once.
        assert "'not_runnable'" in s.wait(lambda: "'not_runnable'" in s.log("worker") and s.log("worker"), 60,
                                          "duplicate delivery to be acknowledged as a no-op")
        assert [a["outcome"] for a in s.attempts(view["job_id"], "prepare")] == ["succeeded"]
        assert [a["outcome"] for a in s.attempts(view["job_id"], "publish")] == ["succeeded"]


# 3a. ASR committed, worker child dies before ACK: redelivery is a no-op, ASR is not repeated.
def test_asr_result_is_kept_when_the_worker_dies_after_commit_before_ack(stack, video):
    s = stack()
    with TestClient(s.app) as client:
        _, view = submit(s, client, video)
        s.worker(VAM_PIPELINE_FAULT="after_commit:asr")
        s.dispatcher()
        assert s.wait_job(view["job_id"])["status"] == "succeeded"
        assert (s.markers / "after_commit_asr").exists()
        assert calls(s.counters, "transcribe") == 1
        assert [a["outcome"] for a in s.attempts(view["job_id"], "asr")] == ["succeeded"]
        # Celery reported the lost pool child and (reject_on_worker_lost) requeued the message.
        assert re.search(r"WorkerLostError|Process .* exited", s.log("worker"))


# 3b/4a. ASR product uploaded and its checkpoint recorded, worker killed before the stage commit:
#        the restart reuses the verified checkpoint instead of transcribing again.
def test_asr_checkpoint_is_reused_after_a_kill_between_upload_and_commit(stack, video):
    s = stack()
    with TestClient(s.app) as client:
        _, view = submit(s, client, video)
        s.worker(VAM_PIPELINE_FAULT="before_commit:asr")
        s.dispatcher()
        assert s.wait_job(view["job_id"])["status"] == "succeeded"
        attempts = s.attempts(view["job_id"], "asr")
        assert [a["outcome"] for a in attempts] == ["lease_lost", "succeeded"]
        assert calls(s.counters, "transcribe") == 1                      # ASR ran once in total
        assert s.stage(view["job_id"], "asr")["output"]["reused"] is True


# The narrower window between the object upload and the checkpoint row: no reusable product is
# recorded, so ASR runs again (bounded extra cost); the unreferenced object is cleanup debt.
def test_kill_between_asr_upload_and_checkpoint_recomputes_once(stack, video):
    s = stack()
    with TestClient(s.app) as client:
        _, view = submit(s, client, video)
        s.worker(VAM_PIPELINE_FAULT="after_asr_upload")
        s.dispatcher()
        assert s.wait_job(view["job_id"])["status"] == "succeeded"
        assert [a["outcome"] for a in s.attempts(view["job_id"], "asr")] == ["lease_lost", "succeeded"]
        assert calls(s.counters, "transcribe") == 2
        assert s.stage(view["job_id"], "asr")["output"]["reused"] is False


# 4b. Vision index written (attempt-keyed generation), worker killed before commit; and a kill after
#     a commit but before ACK. Both converge; the dead attempt's generation is cleaned up.
def test_products_before_commit_and_commit_before_ack_both_converge(stack, video):
    s = stack()
    with TestClient(s.app) as client:
        alice, view = submit(s, client, video)
        s.worker(VAM_PIPELINE_FAULT="before_commit:vision,after_commit:prepare")
        s.dispatcher()
        assert s.wait_job(view["job_id"])["status"] == "succeeded"
        vision = s.attempts(view["job_id"], "vision")
        assert [a["outcome"] for a in vision] == ["lease_lost", "succeeded"]
        assert [a["outcome"] for a in s.attempts(view["job_id"], "prepare")] == ["succeeded"]
        published = alice.get(f"/api/videos/{view['video_id']}").json()["active_generation"]
        assert published == vision[1]["attempt_token"]
        registry = s.app.state.indexing.registry
        assert not registry.asset(view["video_id"], f"memory/{vision[0]['attempt_token']}.sqlite3").exists()


# 4c. The whole worker (master + children) is SIGKILLed mid-stage: RabbitMQ redelivers the unacked message.
def test_killing_the_whole_worker_mid_stage_redelivers_and_completes(stack, video):
    s = stack()
    with TestClient(s.app) as client:
        _, view = submit(s, client, video)
        first = s.worker("worker-1", VAM_PIPELINE_FAULT="stall:vision")
        s.dispatcher()
        s.wait(lambda: s.stage(view["job_id"], "vision")["status"] == "running", 120, "vision to start")
        s.kill(first)
        s.worker("worker-2")
        assert s.wait_job(view["job_id"])["status"] == "succeeded"
        assert [a["outcome"] for a in s.attempts(view["job_id"], "vision")] == ["lease_lost", "succeeded"]


# 5a. Duplicate dispatches consumed concurrently by two workers: one execution.
def test_duplicate_messages_on_concurrent_workers_execute_once(stack, video):
    s = stack()
    with TestClient(s.app) as client:
        _, view = submit(s, client, video)
        repo = s.app.state.pipeline.repo
        with repo.db.transaction() as conn:
            for _ in range(5):
                repo._dispatch(conn, view["job_id"], "prepare")
        s.worker("worker-a", concurrency=2)
        s.worker("worker-b", concurrency=2)
        s.dispatcher()
        assert s.wait_job(view["job_id"])["status"] == "succeeded"
        assert [a["outcome"] for a in s.attempts(view["job_id"], "prepare")] == ["succeeded"]


# 5b. A hung worker loses its lease; another worker takes over; the late result is rejected.
def test_stale_worker_cannot_overwrite_the_newer_result(stack, video):
    s = stack()
    with TestClient(s.app) as client:
        alice, view = submit(s, client, video)
        s.worker("worker-slow", VAM_PIPELINE_FAULT="stall:vision")
        s.dispatcher()
        s.wait(lambda: s.stage(view["job_id"], "vision")["status"] == "running", 120, "vision to start")
        s.worker("worker-fast", queues="media,asr,embed,control")
        assert s.wait_job(view["job_id"])["status"] == "succeeded"
        s.wait(lambda: "stage_result_discarded" in s.log("worker-slow"), 60, "the stale result to be discarded")
        attempts = s.attempts(view["job_id"], "vision")
        assert [a["outcome"] for a in attempts] == ["lease_lost", "succeeded"]
        assert alice.get(f"/api/videos/{view['video_id']}").json()["active_generation"] == attempts[1]["attempt_token"]
        registry = s.app.state.indexing.registry
        assert not registry.asset(view["video_id"], f"memory/{attempts[0]['attempt_token']}.sqlite3").exists()


# 5c. Broker temporarily unavailable: intents stay in the outbox and are delivered after recovery.
@pytest.mark.skipif(not (os.environ.get("EVIREEL_TEST_BROKER_STOP") and os.environ.get("EVIREEL_TEST_BROKER_START")),
                    reason="needs EVIREEL_TEST_BROKER_STOP/START commands")
def test_broker_outage_delays_but_does_not_lose_work(stack, video):
    s = stack()
    with TestClient(s.app) as client:
        subprocess.run(os.environ["EVIREEL_TEST_BROKER_STOP"], shell=True, check=True, timeout=120)
        try:
            _, view = submit(s, client, video)
            s.dispatcher()
            repo = s.app.state.pipeline.repo

            def failing_publishes():
                with repo.db.connect() as conn:
                    return conn.execute("SELECT attempts FROM outbox_events WHERE status='pending' "
                                        "AND attempts >= 2").fetchone()
            s.wait(failing_publishes, 90, "publish attempts to fail while the broker is down")
        finally:
            subprocess.run(os.environ["EVIREEL_TEST_BROKER_START"], shell=True, check=True, timeout=180)
        s.worker()
        assert s.wait_job(view["job_id"], timeout=300)["status"] == "succeeded"
        with repo.db.connect() as conn:
            row = conn.execute("SELECT * FROM outbox_events WHERE stage='prepare' ORDER BY event_id LIMIT 1").fetchone()
        assert row["status"] == "sent" and row["attempts"] >= 3 and row["last_error"]


# 5d. A stage that keeps failing transiently stops after the attempt limit; the other branch still publishes.
def test_retries_are_bounded_and_the_failure_is_recorded(stack, video):
    s = stack(pipeline_stage_max_attempts=2)
    with TestClient(s.app) as client:
        alice, view = submit(s, client, video)
        s.worker(E2_ASR="transient")
        s.dispatcher()
        job = s.wait_job(view["job_id"])
        assert job["status"] == "partial"
        assert [(a["outcome"], a["error_code"]) for a in s.attempts(view["job_id"], "asr")] == [
            ("retry", "asr_timeout"), ("failed", "asr_timeout")]
        assert s.stage(view["job_id"], "transcript_index")["status"] == "skipped"
        detail = alice.get(f"/api/videos/{view['video_id']}").json()
        assert detail["capabilities"]["vision"] is True and detail["capabilities"]["transcript"] is False
        ledger = alice.get("/api/pipeline/failures").json()["items"]
        assert {(f["stage"], f["outcome"]) for f in ledger} >= {("asr", "retry"), ("asr", "failed")}


# 6. A failing branch on re-index keeps the old index and other capabilities; progress is queryable.
def test_partial_reindex_keeps_previous_index_and_reports_progress(stack, video):
    s = stack(pipeline_stage_max_attempts=1)
    with TestClient(s.app) as client:
        alice, view = submit(s, client, video)
        first = s.worker("worker-1")
        s.dispatcher()
        assert s.wait_job(view["job_id"])["status"] == "succeeded"
        before = alice.get(f"/api/videos/{view['video_id']}").json()
        s.kill(first)
        s.worker("worker-2", E2_VISION="fail")
        job_id = alice.post(f"/api/videos/{view['video_id']}/index").json()["job_id"]
        assert s.wait_job(job_id)["status"] == "partial"
        after = alice.get(f"/api/videos/{view['video_id']}").json()
        assert after["active_generation"] == before["active_generation"]
        assert after["capabilities"] == before["capabilities"]
        progress = alice.get(f"/api/jobs/{job_id}").json()
        assert progress["status"] == "partial"
        assert {st["stage"]: st["status"] for st in progress["pipeline"]["stages"]}["vision"] == "failed"


# Long stage vs. Celery time limits: soft limit -> recorded retryable timeout; work then completes.
def test_long_stage_hits_the_soft_time_limit_and_is_retried(stack, video):
    s = stack(pipeline_task_time_limit_s=30, pipeline_lease_s=20)
    with TestClient(s.app) as client:
        _, view = submit(s, client, video)
        s.worker(VAM_PIPELINE_FAULT="stall:vision")   # one-shot stall of 60 s > 25 s soft limit
        s.dispatcher()
        assert s.wait_job(view["job_id"], timeout=300)["status"] == "succeeded"
        vision = s.attempts(view["job_id"], "vision")
        assert [(a["outcome"], a["error_code"]) for a in vision] == [("retry", "stage_timeout"), ("succeeded", None)]
        assert "SoftTimeLimitExceeded" in s.log("worker") or "soft time limit" in s.log("worker").lower()
