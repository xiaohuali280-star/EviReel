"""API-side pipeline client (worker mode): durable acceptance only. It validates, writes the job,
its stages and the first outbox row in one PostgreSQL transaction, mirrors the job into the
SQLite read model, and returns. Nothing here runs media or model work."""
from __future__ import annotations

from vam.pipeline.repository import JobRepository


def _jsonable(row):
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(row).items()}


class PipelineClient:
    def __init__(self, db, registry, settings):
        self.repo = JobRepository(db, settings)
        self.db, self.registry = db, registry

    def submit(self, video_id, *, kind="video", request_id=None, params=None) -> str:
        with self.db.connect() as conn:
            owned = conn.execute("SELECT workspace_id FROM video_ownership WHERE video_id=%s", (video_id,)).fetchone()
        job, _ = self.repo.submit(video_id=video_id, kind=kind, workspace_id=owned["workspace_id"] if owned else None,
                                  request_id=request_id, params=params)
        # Read-model mirror. If this process dies right here, the worker's first claim re-creates it.
        self.registry.project_job(job["job_id"], video_id, kind)
        return job["job_id"]

    def retry(self, job_id) -> str:
        """Manual retry: a new job for the same video/kind. Idempotent per failed job; verified
        checkpoints (ASR output, embedding batches) make the rerun skip finished expensive work."""
        job = self.repo.job(job_id)
        if job["status"] not in {"failed", "partial"}:
            from vam.video_registry import DomainError
            raise DomainError(409, "job_not_retryable", "Only failed or partial jobs can be retried")
        return self.submit(job["video_id"], kind=job["kind"], request_id=f"retry:{job_id}")

    def describe(self, job_id) -> dict:
        job = self.repo.job(job_id)
        stages = self.repo.stages(job_id)
        return {"job": _jsonable(job), "stages": [_jsonable(s) | {"output": s["output"]} for s in stages.values()],
                "attempts": [_jsonable(a) for a in self.repo.attempts(job_id)]}

    def has_active(self, video_id) -> bool:
        return self.repo.active_for_video(video_id) is not None

    def failures(self, workspace_ids, limit=50):
        return [_jsonable(r) for r in self.repo.failures(workspace_ids, limit)]
