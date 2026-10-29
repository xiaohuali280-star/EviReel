"""Job/stage ledger in PostgreSQL: the only place pipeline correctness is decided.

Protocol (at-least-once delivery, idempotent effects):
* submit: job + stages + outbox rows in one transaction; (workspace, request_id) is unique and the
  params digest separates "same request" (returns the job) from "different request" (409).
* claim: row lock on the stage; only a runnable stage (pending / due retry_wait / running with an
  expired lease) is claimed. A claim mints a fresh attempt_token and lease; duplicate messages
  for succeeded/running stages are no-ops.
* complete / fail: conditional on (status='running', attempt_token=<mine>); a worker whose lease
  was taken over gets False and its result is discarded. Completion, successor activation and
  their outbox rows commit together, under the job row lock (serializes the branch join).
* scan: re-dispatches stages whose lease expired or whose dispatch was never claimed, and fails
  stages that used up their attempts. Messages are hints; this scan is what guarantees progress.
"""
from __future__ import annotations

import hashlib
import json
import random
import uuid

from vam.pipeline.graph import GRAPHS, JOIN_STAGES, QUEUES, TERMINAL
from vam.video_registry import DomainError

FINAL_JOB = {"succeeded", "partial", "failed"}


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(value) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else str(value).encode()).hexdigest()


class StaleAttempt(Exception):
    """The caller's attempt token is no longer current (lease lost / stage re-claimed)."""


class JobRepository:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    # ------------------------------------------------------------------ helpers
    def backoff_s(self, attempt: int) -> float:
        # Exponential with "equal jitter": half fixed, half random, capped.
        base = min(self.settings.pipeline_retry_cap_s, self.settings.pipeline_retry_base_s * 2 ** max(attempt - 1, 0))
        return base / 2 + random.uniform(0, base / 2)

    @staticmethod
    def _dispatch(conn, job_id, stage, *, delay_s=0.0):
        """Record a dispatch intent (outbox row) for a stage, in the caller's transaction."""
        row = conn.execute(
            "UPDATE job_stages SET dispatches=dispatches+1, last_dispatch_at=now() WHERE job_id=%s AND stage=%s "
            "RETURNING dispatches", (job_id, stage)).fetchone()
        conn.execute(
            "INSERT INTO outbox_events(dedupe_key, job_id, stage, queue, next_attempt_at) "
            "VALUES (%s,%s,%s,%s, now() + make_interval(secs => %s)) ON CONFLICT (dedupe_key) DO NOTHING",
            (f"{job_id}:{stage}:{row['dispatches']}", job_id, stage, QUEUES[stage], delay_s))

    def job(self, job_id, conn=None):
        def load(c):
            row = c.execute("SELECT * FROM jobs WHERE job_id=%s", (job_id,)).fetchone()
            if row is None:
                raise DomainError(404, "job_not_found", "Job not found")
            return row
        if conn is not None:
            return load(conn)
        with self.db.connect() as c:
            return load(c)

    def stages(self, job_id, conn=None):
        sql = "SELECT * FROM job_stages WHERE job_id=%s ORDER BY stage"
        if conn is not None:
            return {r["stage"]: r for r in conn.execute(sql, (job_id,))}
        with self.db.connect() as c:
            return {r["stage"]: r for r in c.execute(sql, (job_id,))}

    def attempts(self, job_id):
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM stage_attempts WHERE job_id=%s ORDER BY started_at", (job_id,)).fetchall()

    # ------------------------------------------------------------------ submit
    def submit(self, *, video_id, kind, workspace_id=None, request_id=None, params=None):
        if kind not in GRAPHS:
            raise DomainError(422, "invalid_job_kind", "Unsupported job kind")
        params = {"kind": kind, "video_id": video_id, **(params or {})}
        digest = sha256_hex(canonical(params))
        import psycopg
        for attempt in range(2):  # a unique-violation race resolves to the winner's row on the second pass
            try:
                with self.db.transaction() as conn:
                    if request_id is not None:
                        existing = conn.execute(
                            "SELECT * FROM jobs WHERE COALESCE(workspace_id,'')=COALESCE(%s,'') AND request_id=%s",
                            (workspace_id, request_id)).fetchone()
                        if existing is not None:
                            if existing["params_digest"] != digest:
                                raise DomainError(409, "request_conflict",
                                                  "This request id was already used with different parameters")
                            return existing, False
                    return self._insert_job(conn, video_id, kind, workspace_id, request_id, params, digest), True
            except psycopg.errors.UniqueViolation as exc:
                # With a request id, a concurrent twin may have won either index: look it up once more.
                if request_id is not None and attempt == 0:
                    continue
                if "jobs_one_active" in str(exc):
                    raise DomainError(409, "index_running", "This video already has an indexing job") from exc
                raise
        raise DomainError(409, "request_conflict", "Concurrent submission with this request id; retry")

    def _insert_job(self, conn, video_id, kind, workspace_id, request_id, params, digest):
        job_id = uuid.uuid4().hex
        job = conn.execute(
            "INSERT INTO jobs(job_id, workspace_id, video_id, kind, request_id, params, params_digest, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'queued') RETURNING *",
            (job_id, workspace_id, video_id, kind, request_id, json.dumps(params), digest)).fetchone()
        graph = GRAPHS[kind]
        for stage, deps in graph.items():
            conn.execute("INSERT INTO job_stages(job_id, stage, status) VALUES (%s,%s,%s)",
                         (job_id, stage, "waiting" if deps else "pending"))
        for stage, deps in graph.items():
            if not deps:
                self._dispatch(conn, job_id, stage)
        return job

    def _follow_up(self, conn, job_id, follow):
        """A job created by a stage completion (e.g. auto notes after publish), in the same transaction.
        Runs in a savepoint: a conflict (request id reused, another active job) never aborts the commit."""
        import psycopg
        parent = conn.execute("SELECT video_id, workspace_id FROM jobs WHERE job_id=%s", (job_id,)).fetchone()
        params = {"kind": follow["kind"], "video_id": parent["video_id"], **follow.get("params", {})}
        try:
            with conn.transaction():
                if conn.execute("SELECT 1 FROM jobs WHERE COALESCE(workspace_id,'')=COALESCE(%s,'') AND request_id=%s",
                                (parent["workspace_id"], follow["request_id"])).fetchone():
                    return
                self._insert_job(conn, parent["video_id"], follow["kind"], parent["workspace_id"], follow["request_id"],
                                 params, sha256_hex(canonical(params)))
        except psycopg.errors.UniqueViolation:
            pass   # another job is active for this video; the user can start notes later

    # ------------------------------------------------------------------ claim / heartbeat
    def claim(self, job_id, stage, worker_id):
        """Returns {"stage": row, "job": row} with a fresh attempt token, or None if not runnable."""
        lease = self.settings.pipeline_lease_s
        with self.db.transaction() as conn:
            # Lock order everywhere: job row, then stage rows (complete/fail/scan do the same).
            if conn.execute("SELECT 1 FROM jobs WHERE job_id=%s FOR UPDATE", (job_id,)).fetchone() is None:
                return None
            row = conn.execute(
                "SELECT s.*, (s.lease_until < now()) AS expired, (s.next_attempt_at <= now()) AS due "
                "FROM job_stages s WHERE job_id=%s AND stage=%s FOR UPDATE", (job_id, stage)).fetchone()
            if row is None:
                return None
            runnable = (row["status"] == "pending" or (row["status"] == "retry_wait" and row["due"])
                        or (row["status"] == "running" and row["expired"]))
            if not runnable:
                return None  # duplicate / early / someone else holds a live lease
            if row["status"] == "running":   # taking over from a worker that stopped heartbeating
                conn.execute("UPDATE stage_attempts SET outcome='lease_lost', ended_at=now(), error_code='lease_expired' "
                             "WHERE attempt_token=%s AND outcome='running'", (row["attempt_token"],))
            if row["attempts"] >= self.settings.pipeline_stage_max_attempts:
                self._finish_failed(conn, job_id, stage, "attempts_exhausted",
                                    f"Stage stopped after {row['attempts']} attempts")
                return None
            token = uuid.uuid4().hex
            claimed = conn.execute(
                "UPDATE job_stages SET status='running', attempts=attempts+1, attempt_token=%s, worker_id=%s, "
                "lease_until=now() + make_interval(secs => %s), heartbeat_at=now(), started_at=COALESCE(started_at, now()), "
                "error_code=NULL, error=NULL WHERE job_id=%s AND stage=%s RETURNING *",
                (token, worker_id, lease, job_id, stage)).fetchone()
            conn.execute("INSERT INTO stage_attempts(attempt_token, job_id, stage, attempt, worker_id, outcome) "
                         "VALUES (%s,%s,%s,%s,%s,'running')", (token, job_id, stage, claimed["attempts"], worker_id))
            job = conn.execute("UPDATE jobs SET status='running', updated_at=now() WHERE job_id=%s AND status='queued' "
                               "RETURNING *", (job_id,)).fetchone() or self.job(job_id, conn)
            return {"stage": claimed, "job": job}

    def heartbeat(self, job_id, stage, token) -> bool:
        with self.db.transaction() as conn:
            return bool(conn.execute(
                "UPDATE job_stages SET lease_until=now() + make_interval(secs => %s), heartbeat_at=now() "
                "WHERE job_id=%s AND stage=%s AND status='running' AND attempt_token=%s RETURNING 1",
                (self.settings.pipeline_lease_s, job_id, stage, token)).fetchone())

    # ------------------------------------------------------------------ outcomes
    def _owned(self, conn, job_id, stage, token):
        conn.execute("SELECT 1 FROM jobs WHERE job_id=%s FOR UPDATE", (job_id,))  # serializes joins per job
        row = conn.execute("SELECT * FROM job_stages WHERE job_id=%s AND stage=%s AND status='running' "
                           "AND attempt_token=%s FOR UPDATE", (job_id, stage, token)).fetchone()
        if row is None:
            conn.execute("UPDATE stage_attempts SET outcome='lease_lost', ended_at=now(), "
                         "error_code=COALESCE(error_code,'lease_lost') WHERE attempt_token=%s AND outcome='running'", (token,))
        return row

    def complete(self, job_id, stage, token, output, *, skip=(), job_result=None, follow_up=()) -> bool:
        """Commit a stage result, activate successors and record their dispatch, atomically."""
        with self.db.transaction() as conn:
            if self._owned(conn, job_id, stage, token) is None:
                return False
            conn.execute("UPDATE job_stages SET status='succeeded', output=%s, finished_at=now(), lease_until=NULL "
                         "WHERE job_id=%s AND stage=%s", (json.dumps(output), job_id, stage))
            conn.execute("UPDATE stage_attempts SET outcome='succeeded', ended_at=now() WHERE attempt_token=%s", (token,))
            for name, reason in dict(skip).items():
                conn.execute("UPDATE job_stages SET status='skipped', error_code=%s, finished_at=now() "
                             "WHERE job_id=%s AND stage=%s AND status='waiting'", (reason, job_id, name))
            if job_result is not None:
                self._finish_job(conn, job_id, job_result)
            self._advance(conn, job_id)
            for follow in follow_up:
                self._follow_up(conn, job_id, follow)
            return True

    def fail(self, job_id, stage, token, *, code, message, transient) -> str:
        """'retry' (scheduled with backoff), 'failed' (terminal), or 'lost' (not our attempt any more)."""
        with self.db.transaction() as conn:
            row = self._owned(conn, job_id, stage, token)
            if row is None:
                return "lost"
            message = str(message)[:800]
            if transient and row["attempts"] < self.settings.pipeline_stage_max_attempts:
                delay = self.backoff_s(row["attempts"])
                conn.execute("UPDATE job_stages SET status='retry_wait', lease_until=NULL, error_code=%s, error=%s, "
                             "next_attempt_at=now() + make_interval(secs => %s) WHERE job_id=%s AND stage=%s",
                             (code, message, delay, job_id, stage))
                conn.execute("UPDATE stage_attempts SET outcome='retry', ended_at=now(), error_code=%s, error=%s "
                             "WHERE attempt_token=%s", (code, message, token))
                self._dispatch(conn, job_id, stage, delay_s=delay)
                return "retry"
            conn.execute("UPDATE stage_attempts SET outcome='failed', ended_at=now(), error_code=%s, error=%s "
                         "WHERE attempt_token=%s", (code, message, token))
            self._finish_failed(conn, job_id, stage, code, message)
            return "failed"

    def _finish_failed(self, conn, job_id, stage, code, message):
        conn.execute("SELECT 1 FROM jobs WHERE job_id=%s FOR UPDATE", (job_id,))
        conn.execute("UPDATE job_stages SET status='failed', lease_until=NULL, error_code=%s, error=%s, finished_at=now() "
                     "WHERE job_id=%s AND stage=%s AND status NOT IN ('succeeded','failed','skipped')",
                     (code, message, job_id, stage))
        if stage in JOIN_STAGES:  # nothing will publish a result: the job ends failed
            self._finish_job(conn, job_id, {"status": "failed", "error_code": code, "error": message})
        self._advance(conn, job_id)

    @staticmethod
    def _finish_job(conn, job_id, result):
        conn.execute("UPDATE jobs SET status=%s, result=%s, error_code=%s, error=%s, finished_at=now(), updated_at=now() "
                     "WHERE job_id=%s AND status IN ('queued','running')",
                     (result["status"], json.dumps(result.get("result")), result.get("error_code"), result.get("error"), job_id))

    def _advance(self, conn, job_id):
        """Activate or skip waiting stages whose dependencies are terminal (idempotent, under the job lock)."""
        kind = conn.execute("SELECT kind FROM jobs WHERE job_id=%s", (job_id,)).fetchone()["kind"]
        graph = GRAPHS[kind]
        changed = True
        while changed:
            changed = False
            status = {r["stage"]: r["status"] for r in conn.execute(
                "SELECT stage, status FROM job_stages WHERE job_id=%s", (job_id,))}
            for stage, deps in graph.items():
                if status[stage] != "waiting" or not all(status[d] in TERMINAL for d in deps):
                    continue
                if stage in JOIN_STAGES or all(status[d] == "succeeded" for d in deps):
                    if conn.execute("UPDATE job_stages SET status='pending', next_attempt_at=now() "
                                    "WHERE job_id=%s AND stage=%s AND status='waiting' RETURNING 1",
                                    (job_id, stage)).fetchone():
                        self._dispatch(conn, job_id, stage)
                else:
                    conn.execute("UPDATE job_stages SET status='skipped', error_code='dependency_not_succeeded', "
                                 "finished_at=now() WHERE job_id=%s AND stage=%s AND status='waiting'", (job_id, stage))
                changed = True
        # Graphs without a join (notes, export) end when every stage is terminal.
        rows = conn.execute("SELECT stage, status, error_code, error FROM job_stages WHERE job_id=%s ORDER BY finished_at",
                            (job_id,)).fetchall()
        if all(r["status"] in TERMINAL for r in rows):
            failed = next((r for r in rows if r["status"] == "failed"), None)
            self._finish_job(conn, job_id, {"status": "failed", "error_code": failed["error_code"], "error": failed["error"]}
                             if failed else {"status": "succeeded"})

    # ------------------------------------------------------------------ recovery
    def scan(self, _after_select=None) -> dict:
        """Re-dispatch stalled work; fail stages that used up their attempts. Safe to run anywhere, repeatedly.

        Candidates are only hints read without locks: every decision is re-checked under the row locks
        (still running, same attempt token, lease still expired / still undelivered), so a heartbeat or
        a takeover that happens after the candidate read is never overruled. `_after_select` is a test
        hook that runs between the candidate read and the locked re-check."""
        s = self.settings
        summary = {"lease_expired": 0, "redispatched": 0, "exhausted": 0}
        with self.db.connect() as conn:
            expired = conn.execute(
                "SELECT job_id, stage, attempts, attempt_token FROM job_stages WHERE status='running' AND lease_until < now() "
                "AND (last_dispatch_at IS NULL OR last_dispatch_at < now() - make_interval(secs => %s))",
                (s.pipeline_lease_s,)).fetchall()
            stale = conn.execute(
                "SELECT job_id, stage FROM job_stages WHERE status='pending' "
                "AND (last_dispatch_at IS NULL OR last_dispatch_at < now() - make_interval(secs => %s)) "
                "AND NOT EXISTS (SELECT 1 FROM outbox_events o WHERE o.job_id=job_stages.job_id "
                "                AND o.stage=job_stages.stage AND o.status='pending')",
                (s.pipeline_redispatch_after_s,)).fetchall()
        if _after_select:
            _after_select(expired, stale)
        for row in expired:
            with self.db.transaction() as conn:
                conn.execute("SELECT 1 FROM jobs WHERE job_id=%s FOR UPDATE", (row["job_id"],))
                current = conn.execute(
                    "SELECT * FROM job_stages WHERE job_id=%s AND stage=%s AND status='running' AND attempt_token=%s "
                    "AND lease_until < now() AND (last_dispatch_at IS NULL OR last_dispatch_at < now() - "
                    "make_interval(secs => %s)) FOR UPDATE",
                    (row["job_id"], row["stage"], row["attempt_token"], s.pipeline_lease_s)).fetchone()
                if current is None:
                    continue   # renewed, taken over, finished or already re-dispatched since the candidate read
                if current["attempts"] >= s.pipeline_stage_max_attempts:
                    conn.execute("UPDATE stage_attempts SET outcome='lease_lost', ended_at=now(), error_code='lease_expired' "
                                 "WHERE attempt_token=%s AND outcome='running'", (current["attempt_token"],))
                    self._finish_failed(conn, row["job_id"], row["stage"], "attempts_exhausted",
                                        f"Worker stopped responding on all {current['attempts']} attempts")
                    summary["exhausted"] += 1
                else:
                    self._dispatch(conn, row["job_id"], row["stage"])
                    summary["lease_expired"] += 1
        for row in stale:
            with self.db.transaction() as conn:
                conn.execute("SELECT 1 FROM jobs WHERE job_id=%s FOR UPDATE", (row["job_id"],))
                if conn.execute(
                        "SELECT 1 FROM job_stages WHERE job_id=%s AND stage=%s AND status='pending' "
                        "AND (last_dispatch_at IS NULL OR last_dispatch_at < now() - make_interval(secs => %s)) "
                        "AND NOT EXISTS (SELECT 1 FROM outbox_events o WHERE o.job_id=%s AND o.stage=%s AND o.status='pending') "
                        "FOR UPDATE", (row["job_id"], row["stage"], s.pipeline_redispatch_after_s,
                                       row["job_id"], row["stage"])).fetchone():
                    self._dispatch(conn, row["job_id"], row["stage"])
                    summary["redispatched"] += 1
        return summary

    # ------------------------------------------------------------------ outbox
    def publish_due(self, send, limit=100) -> dict:
        """Publish due outbox rows with `send(row)` (must raise unless the broker confirmed).

        Rows stay locked (SKIP LOCKED for other dispatchers) while sending; a crash after the broker
        confirmed but before this commit re-sends them later - consumers are idempotent."""
        sent = failed = 0
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT * FROM outbox_events WHERE status='pending' AND next_attempt_at <= now() "
                                "ORDER BY next_attempt_at, event_id LIMIT %s FOR UPDATE SKIP LOCKED", (limit,)).fetchall()
            for row in rows:
                try:
                    send(row)
                except Exception as exc:  # broker down / nack: keep the intent, retry with backoff
                    failed += 1
                    conn.execute("UPDATE outbox_events SET attempts=attempts+1, last_error=%s, "
                                 "next_attempt_at=now() + make_interval(secs => %s) WHERE event_id=%s",
                                 (f"{type(exc).__name__}: {exc}"[:500], min(60.0, 0.5 * 2 ** min(row["attempts"], 7)),
                                  row["event_id"]))
                    continue
                conn.execute("UPDATE outbox_events SET status='sent', sent_at=now(), attempts=attempts+1 "
                             "WHERE event_id=%s", (row["event_id"],))
                sent += 1
        return {"sent": sent, "failed": failed}

    # ------------------------------------------------------------------ checkpoints
    def checkpoint(self, reuse_key):
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM checkpoints WHERE reuse_key=%s", (reuse_key,)).fetchone()

    def save_checkpoint(self, reuse_key, object_key, sha256, size, token) -> dict:
        """First verified product wins; returns the winning row (a loser's object becomes cleanup debt)."""
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO checkpoints(reuse_key, object_key, sha256, size_bytes, attempt_token) "
                         "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (reuse_key) DO NOTHING",
                         (reuse_key, object_key, sha256, size, token))
            return conn.execute("SELECT * FROM checkpoints WHERE reuse_key=%s", (reuse_key,)).fetchone()

    def drop_checkpoint(self, reuse_key, object_key):
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM checkpoints WHERE reuse_key=%s AND object_key=%s", (reuse_key, object_key))

    # ------------------------------------------------------------------ queries
    def active_for_video(self, video_id):
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM jobs WHERE video_id=%s AND status IN ('queued','running')",
                                (video_id,)).fetchone()

    def failures(self, workspace_ids, limit=50):
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT a.*, j.video_id, j.kind, j.workspace_id, s.status AS stage_status FROM stage_attempts a "
                "JOIN jobs j USING (job_id) JOIN job_stages s USING (job_id, stage) "
                "WHERE a.outcome IN ('failed','retry','lease_lost') AND j.workspace_id = ANY(%s) "
                "ORDER BY a.started_at DESC LIMIT %s", (list(workspace_ids), limit)).fetchall()
