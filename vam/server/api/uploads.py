from typing import Annotated

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import Field, StringConstraints

from vam.protocol import StrictSchemaModel
from vam.runtime import run_sync
from vam.server.platform_auth import principal_of
from vam.video_registry import DomainError

router = APIRouter(prefix="/api/uploads", tags=["Resumable uploads"])
UploadId = Annotated[str, StringConstraints(pattern=r"^up_[0-9a-f]{32}$")]


class CreateUpload(StrictSchemaModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    fingerprint: str = Field(min_length=8, max_length=128)
    content_type: str = Field("", max_length=120)


class PartToSign(StrictSchemaModel):
    part_number: int = Field(ge=1, le=10000)
    size: int = Field(gt=0)
    md5_b64: str = Field(min_length=24, max_length=24)


class SignParts(StrictSchemaModel):
    parts: list[PartToSign] = Field(min_length=1, max_length=100)


def _uploads(request: Request):
    platform = getattr(request.app.state, "platform", None)
    if platform is None:
        raise DomainError(404, "resumable_upload_disabled",
                          "Resumable upload requires platform mode (DATABASE_URL and object storage)")
    return platform.uploads


@router.post("", status_code=201)
async def create(body: CreateUpload, request: Request):
    return await run_sync(_uploads(request).create, principal_of(request), name=body.filename, size=body.size,
                          fingerprint=body.fingerprint, content_type=body.content_type)


@router.get("")
async def active(request: Request):
    return {"items": await run_sync(_uploads(request).list_active, principal_of(request))}


@router.get("/{upload_id}")
async def progress(upload_id: UploadId, request: Request):
    return await run_sync(_uploads(request).get, principal_of(request), upload_id)


@router.post("/{upload_id}/parts/sign")
async def sign(upload_id: UploadId, body: SignParts, request: Request):
    return await run_sync(_uploads(request).sign, principal_of(request), upload_id,
                          [p.model_dump() for p in body.parts])


@router.post("/{upload_id}/parts/{part_number}/done")
async def part_done(upload_id: UploadId, part_number: int, request: Request):
    """Optional progress registration; storage (ListParts) stays authoritative, so a lost call is harmless."""
    return await run_sync(_uploads(request).get, principal_of(request), upload_id)


@router.post("/{upload_id}/complete")
async def complete(upload_id: UploadId, request: Request):
    view = await _uploads(request).complete(principal_of(request), upload_id)
    return JSONResponse(view, status_code=200 if view["state"] == "completed" else 202)


@router.delete("/{upload_id}")
async def cancel(upload_id: UploadId, request: Request):
    return await run_sync(_uploads(request).cancel, principal_of(request), upload_id)
