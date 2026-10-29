"""Claim -> heartbeat -> stage body -> conditional commit. Never raises to the broker: every outcome
is recorded in the ledger first, so the message can always be acknowledged afterwards."""
from __future__ import annotations

import logging
import threading

from vam.pipeline.context import StageError, fault, fault_once
from vam.pipeline.stages import STAGES
from vam.platform.object_store import ObjectStoreError
from vam.video_registry import DomainError

logger = logging.getLogger(__name__)


class Heartbeat(threading.Thread):
    """Extends the lease every lease/3 while the stage runs; notices when the lease was taken over."""
    def __init__(self, repo, job_id, stage, token, interval):
        super().__init__(daemon=True, name=f"heartbeat-{stage}")
        self.repo, self.job_id, self.stage, self.token, self.interval = repo, job_id, stage, token, interval
        self.stopped, self.lost, self.paused = threading.Event(), False, False

    def run(self):
        while not self.stopped.wait(self.interval):
            if self.paused:
                continue  # test hook: a hung worker stops renewing its lease
            try:
                if not self.repo.heartbeat(self.job_id, self.stage, self.token):
                    self.lost = True
                    return
            except Exception:  # database blip: the next beat retries; the lease covers the gap
                logger.warning("heartbeat_failed job_id=%s stage=%s", self.job_id, self.stage, exc_info=True)


def classify(exc) -> StageError:
    if isinstance(exc, StageError):
        return exc
    if type(exc).__name__ == "SoftTimeLimitExceeded":   # Celery soft limit: graceful, retryable
        return StageError("stage_timeout", "Stage exceeded its soft time limit", transient=True)
    if isinstance(exc, DomainError):          # validation-style errors do not improve on retry
        return StageError(exc.code, exc.message, transient=exc.status >= 500)
    if isinstance(exc, (ObjectStoreError, ConnectionError, TimeoutError, OSError)):
        return StageError("transient_io", f"{type(exc).__name__}: {exc}", transient=True)
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status in {408, 409, 425, 429} or (isinstance(status, int) and status >= 500):
        return StageError("provider_unavailable", f"{type(exc).__name__}: {exc}", transient=True)
    # Unknown failures are retried a bounded number of times, then recorded as permanent.
    return StageError("stage_error", f"{type(exc).__name__}: {exc}", transient=True)


def execute(ctx, job_id, stage) -> str:
    claim = ctx.repo.claim(job_id, stage, ctx.worker_id)
    if claim is None:
        return "not_runnable"   # duplicate delivery, early retry, or another worker holds the lease
    token = claim["stage"]["attempt_token"]
    job = claim["job"]
    logger.info("stage_start job_id=%s stage=%s attempt=%s token=%s", job_id, stage, claim["stage"]["attempts"], token)
    beat = Heartbeat(ctx.repo, job_id, stage, token, max(1.0, ctx.settings.pipeline_lease_s / 3))
    beat.start()
    try:
        try:
            ctx.project(job, status="running", phase=stage)
            if stage == "prepare":
                ctx.registry.update_video(job["video_id"], status="running", last_error=None)
            fault(f"before_run:{stage}")
            if fault_once(f"stall:{stage}"):          # hung worker: no heartbeats, outlives its lease
                beat.paused = True
                beat.stopped.wait(ctx.settings.pipeline_lease_s * 3)
                beat.paused = False
            result = STAGES[stage](ctx, claim)
        except Exception as exc:
            error = classify(exc)
            logger.warning("stage_failed job_id=%s stage=%s code=%s transient=%s", job_id, stage, error.code, error.transient)
            if stage == "vision":
                ctx.drop_generation(job["video_id"], token)
            if stage == "publish" and not (error.transient and claim["stage"]["attempts"] < ctx.settings.pipeline_stage_max_attempts):
                ctx.registry.update_job(job_id, status="failed", phase="failed", error_code=error.code, error=error.message)
                ctx.registry.update_video(job["video_id"], status="failed", last_error="Indexing failed; see job details")
            outcome = ctx.repo.fail(job_id, stage, token, code=error.code, message=error.message, transient=error.transient)
            return outcome
        fault(f"before_commit:{stage}")
        committed = ctx.repo.complete(job_id, stage, token, result.output, skip=result.skip, job_result=result.job_result)
        if not committed:
            # Our lease was taken over: another attempt owns the stage. Discard our (attempt-keyed) products.
            logger.warning("stage_result_discarded job_id=%s stage=%s token=%s", job_id, stage, token)
            if stage == "vision":
                ctx.drop_generation(job["video_id"], token)
            return "lost"
        fault(f"after_commit:{stage}")
        return "succeeded"
    finally:
        beat.stopped.set()
        ctx.drop_workdir(token)
