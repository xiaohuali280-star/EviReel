from __future__ import annotations

import base64

from fastapi import APIRouter, Request, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from vam.retrieval.agent_search import hybrid_event_frame_search

from vam.video_registry import DomainError

router = APIRouter(prefix="/api/videos", tags=["Video Memory"])

class VisualSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    query: str = Field(..., min_length=1, max_length=4000)
    top_k: int = Field(5, ge=1, le=50)
    min_time: float = Field(0, ge=0)
    max_time: float | None = Field(None, ge=0)

    @model_validator(mode="after")
    def validate_range(self):
        if not self.query.strip():
            raise ValueError("query is empty")
        if self.max_time is not None and self.max_time < self.min_time:
            raise ValueError("max_time must be >= min_time")
        return self

@router.post("/{video_id}/search")
async def visual_search(video_id: str, body: VisualSearchRequest, request: Request):
    async with request.app.state.indexing.memory_context(video_id) as (row, store):
        hits = await hybrid_event_frame_search(body.query, top_k=body.top_k, store_instance=store,
                    min_time=body.min_time, max_time=body.max_time if body.max_time is not None else float("inf"),
                    time_mode="relative")
        for hit in hits:
            hit.pop("image", None)
            hit["video_id"] = video_id
            if hit.get("frame_id"):
                hit["image_url"] = f"/api/videos/{video_id}/frames/{hit['frame_id']}/image?index_version={row['active_generation']}"
        return {"video_id": video_id, "index_version": row["active_generation"], "results": hits}

@router.get("/{video_id}/frames")
async def list_frames(video_id: str, request: Request, offset: int = Query(0, ge=0),
                      limit: int = Query(50, ge=1, le=200), min_time: float = Query(0, ge=0, allow_inf_nan=False),
                      max_time: float | None = Query(None, ge=0, allow_inf_nan=False)):
    if max_time is not None and max_time < min_time:
        raise DomainError(422, "invalid_time_range", "max_time must be >= min_time")
    async with request.app.state.indexing.memory_context(video_id) as (row, store):
        frames = await store.list_frames_in_time_range(min_time=min_time, max_time=max_time if max_time is not None else float("inf"), time_mode="relative")
        return {"video_id": video_id, "index_version": row["active_generation"], "total": len(frames),
                "items": [{"video_id": video_id, "frame_id": f.frame_id, "t": f.t, "absolute_t": f.absolute_t,
                           "memory_tier": f.memory_tier, "event_id": f.event_id,
                           "image_url": f"/api/videos/{video_id}/frames/{f.frame_id}/image?index_version={row['active_generation']}"}
                          for f in frames[offset:offset+limit]]}

@router.get("/{video_id}/frames/{frame_id}/image")
async def frame_image(video_id: str, frame_id: str, request: Request, index_version: str | None = None):
    async with request.app.state.indexing.memory_context(video_id) as (row, store):
        if index_version and index_version != row["active_generation"]:
            raise DomainError(409, "stale_evidence", "Index changed; refresh this evidence")
        frame = await store.get_frame_by_id(frame_id)
        if frame is None:
            raise DomainError(404, "frame_not_found", "Frame not found in this video")
        header, encoded = frame.image_data_uri.split(",", 1)
        return Response(base64.b64decode(encoded, validate=True), media_type=header[5:].split(";")[0])

@router.get("/{video_id}/memory-documents")
async def memory_documents(video_id: str, request: Request, offset: int = Query(0, ge=0),
                           limit: int = Query(50, ge=1, le=200), kind: str | None = Query(None, pattern="^(event|summary)$"),
                           min_time: float = Query(0, ge=0, allow_inf_nan=False), max_time: float | None = Query(None, ge=0, allow_inf_nan=False)):
    if max_time is not None and max_time < min_time:
        raise DomainError(422, "invalid_time_range", "max_time must be >= min_time")
    async with request.app.state.indexing.memory_context(video_id) as (row, store):
        docs = await store.list_memory_documents_in_time_range(min_time=min_time, max_time=max_time if max_time is not None else float("inf"), time_mode="relative", kind=kind)
        items = []
        for doc in docs[offset:offset+limit]:
            items.append({"video_id": video_id, "doc_id": doc.doc_id, "layer": doc.layer, "kind": doc.kind,
                          "text": doc.text, "start_t": doc.start_t, "end_t": doc.end_t,
                          "representative_frame_id": doc.representative_frame_id, "frame_ids": doc.frame_ids})
        return {"video_id": video_id, "index_version": row["active_generation"], "total": len(docs), "items": items}
