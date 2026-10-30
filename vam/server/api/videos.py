from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request, UploadFile, File, Query
from starlette.responses import FileResponse, JSONResponse, Response

from vam.coordination import lease_lost_error
from vam.runtime import run_sync
from vam.server.platform_auth import principal_of
from vam.video_registry import DomainError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Videos"])

class LeasedFileResponse(FileResponse):
    """Streams a file under a read lease. If the lease is lost mid-stream (maximum lifetime, renewal
    failure) sending stops: the connection is cut and the player re-requests with Range, under a new lease."""
    def __init__(self, path, release, **kwargs):
        super().__init__(path, **kwargs)
        self.release = release

    async def __call__(self, scope, receive, send):
        async def guarded(message):
            if message["type"] == "http.response.body" and not getattr(self.release, "valid", True):
                logger.warning("playback_stopped_lease_lost")
                raise lease_lost_error()
            await send(message)
        try:
            await super().__call__(scope, receive, guarded)
        finally:
            await run_sync(self.release)   # a shared (PostgreSQL) lease release is a short DB call

@router.post("/videos", status_code=202)
async def upload_video(request: Request, video: UploadFile = File(...)):
    return await request.app.state.indexing.ingest(video)

@router.get("/videos")
async def list_videos(request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                      q: str = Query('', max_length=200), trash: bool = False):
    platform = getattr(request.app.state, 'platform', None)
    allowed = None
    if platform is not None:
        allowed = await run_sync(platform.identity.owned_video_ids, principal_of(request).workspace_id)
    return request.app.state.indexing.registry.list(offset, limit, q=q.strip(), trash=trash, video_ids=allowed)

@router.post('/videos/{video_id}/trash')
async def trash_video(video_id: str, request: Request):
    return await run_sync(request.app.state.indexing.trash, video_id)

@router.post('/videos/{video_id}/restore')
async def restore_video(video_id: str, request: Request):
    return await run_sync(lambda: request.app.state.indexing.trash(video_id, restore=True))

@router.get("/videos/{video_id}")
async def video_detail(video_id: str, request: Request):
    reg = request.app.state.indexing.registry
    return reg.public(reg.get(video_id)) | {'chat': request.app.state.conversations.actions.capabilities(video_id)}

@router.get("/videos/{video_id}/content")
async def video_content(video_id: str, request: Request):
    path, release = await run_sync(request.app.state.indexing.playback_asset, video_id)
    return LeasedFileResponse(str(path), release, media_type="video/mp4")

@router.delete("/videos/{video_id}")
async def delete_video(video_id: str, request: Request):
    """204: deleted now. 202: tombstoned -- no new reader or job can start; requests already reading finish
    first and any instance's coordination sweeper reclaims the files, ownership and quota afterwards."""
    principal = principal_of(request)
    result = await run_sync(request.app.state.indexing.delete, video_id, principal.user_id if principal else None)
    if result["status"] == "deleting":
        return JSONResponse(result, status_code=202)
    return Response(status_code=204)


def release_platform_video(platform, video_id):
    """Reclaim hook (platform mode): the SQLite row is gone; releasing ownership frees quota and queues the
    object in one transaction. A failed release is retried by the upload reconciler (orphan ownership)."""
    released = platform.identity.release_video(video_id)
    if released and released.get('object_key'):
        try:
            platform.uploads.cleaner.delete_now(released['object_key'])
        except Exception:  # the deletion is already queued; never fail a completed logical delete
            logger.warning("object_delete_deferred video_id=%s", video_id, exc_info=True)

def idempotency_key(request: Request):
    """Optional `Idempotency-Key` header: same key + same parameters -> same job; different -> 409."""
    key = request.headers.get("idempotency-key")
    if key is not None and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", key):
        raise DomainError(422, "invalid_idempotency_key", "Idempotency-Key must be 8-64 letters, digits, '-' or '_'")
    return f"http:{key}" if key else None

@router.post("/videos/{video_id}/index", status_code=202)
async def index_video(video_id: str, request: Request):
    return {"video_id": video_id, "job_id": request.app.state.indexing.schedule(video_id, request_id=idempotency_key(request))}

@router.get("/jobs/{job_id}")
async def job_detail(job_id: str, request: Request):
    pipeline = getattr(request.app.state, "pipeline", None)
    try:
        job = request.app.state.indexing.registry.job(job_id)
    except DomainError:
        if pipeline is None:
            raise
        job = None   # accepted in PostgreSQL; the read-model mirror is created on the first claim
    if pipeline is not None:
        try:
            ledger = await run_sync(pipeline.describe, job_id)
        except DomainError:
            ledger = None   # a job from before worker mode
        if ledger is not None:
            pg = ledger["job"]
            job = (job or {"job_id": job_id, "video_id": pg["video_id"], "kind": pg["kind"], "phase": "queued",
                           "current": 0, "total": 0, "logs": [], "result": None})
            # The ledger is authoritative for the outcome; the mirror only adds progress detail.
            job.update(status=pg["status"] if pg["status"] in {"succeeded", "partial", "failed"} else job.get("status", pg["status"]),
                       pipeline={"stages": ledger["stages"], "attempts": ledger["attempts"]})
    return job

@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, request: Request):
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:
        raise DomainError(404, "pipeline_disabled", "Manual retry requires the worker pipeline (PIPELINE_MODE=worker)")
    return {"retry_of": job_id, "job_id": await run_sync(pipeline.retry, job_id)}

@router.get("/pipeline/failures")
async def pipeline_failures(request: Request, limit: int = Query(50, ge=1, le=200)):
    """Failure ledger of the caller's workspaces: failed, retried and lease-lost stage attempts."""
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:
        raise DomainError(404, "pipeline_disabled", "The failure ledger requires PIPELINE_MODE=worker")
    principal = principal_of(request)
    return {"items": await run_sync(pipeline.failures, sorted(principal.workspaces), limit)}
