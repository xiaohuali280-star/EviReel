from __future__ import annotations

from fastapi import APIRouter, Request, UploadFile, File, Query
from starlette.responses import FileResponse

router = APIRouter(prefix="/api", tags=["Videos"])

class LeasedFileResponse(FileResponse):
    def __init__(self, path, release, **kwargs):
        super().__init__(path, **kwargs)
        self.release = release

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.release()

@router.post("/videos", status_code=202)
async def upload_video(request: Request, video: UploadFile = File(...)):
    return await request.app.state.indexing.ingest(video)

@router.get("/videos")
async def list_videos(request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    return request.app.state.indexing.registry.list(offset, limit)

@router.get("/videos/{video_id}")
async def video_detail(video_id: str, request: Request):
    reg = request.app.state.indexing.registry
    return reg.public(reg.get(video_id))

@router.get("/videos/{video_id}/content")
async def video_content(video_id: str, request: Request):
    path, release = request.app.state.indexing.playback_asset(video_id)
    return LeasedFileResponse(str(path), release, media_type="video/mp4")

@router.delete("/videos/{video_id}", status_code=204)
async def delete_video(video_id: str, request: Request):
    request.app.state.indexing.delete(video_id)

@router.post("/videos/{video_id}/index", status_code=202)
async def index_video(video_id: str, request: Request):
    return {"video_id": video_id, "job_id": request.app.state.indexing.schedule(video_id)}

@router.get("/jobs/{job_id}")
async def job_detail(job_id: str, request: Request):
    return request.app.state.indexing.registry.job(job_id)
