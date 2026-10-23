from fastapi import APIRouter, Query, Request, Response
import asyncio
from pydantic import Field

from vam.protocol import StrictSchemaModel
from vam.runtime import run_sync
from vam.study_exports import export_study, manuscript
from vam.video_registry import DomainError

router = APIRouter(prefix='/api/videos', tags=['Study'])


class StudyRequest(StrictSchemaModel):
    request_id: str = Field(pattern=r'^r_[0-9a-f]{32}$')


@router.get('/{video_id}/study')
async def state(video_id: str, request: Request):
    return request.app.state.study.state(video_id)


@router.post('/{video_id}/study', status_code=202)
async def generate(video_id: str, body: StudyRequest, request: Request):
    return request.app.state.study.submit(video_id, body.request_id)


@router.get('/{video_id}/study/jobs/{job_id}')
async def job(video_id: str, job_id: str, request: Request):
    return request.app.state.study.job(video_id, job_id)


@router.get('/{video_id}/study/artifacts/{artifact_id}/export')
async def export(video_id: str, artifact_id: str, request: Request, format: str = Query('md', pattern='^(md|pptx|pdf)$')):
    study = request.app.state.study
    artifact = study.artifact(video_id, artifact_id)
    release = study.indexing._retain(video_id)
    try:
        content = await run_sync(export_study, artifact, format, study.settings.study_max_export_pages)
    finally:
        release()
    media = {'md': 'text/markdown; charset=utf-8', 'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation', 'pdf': 'application/pdf'}[format]
    return Response(content, media_type=media, headers={'Content-Disposition': f'attachment; filename="EviReel-{artifact_id}.{format}"'})


@router.get('/{video_id}/transcripts/export')
async def export_manuscript(video_id: str, request: Request, format: str = Query('txt', pattern='^(txt|srt)$'),
                            version: str | None = Query(None, pattern=r'^[0-9a-f]{32,64}$')):
    service = request.app.state.indexing
    lock = service.memory_locks.setdefault(video_id, asyncio.Lock())
    async with lock:
        async with service.transcript_context(video_id) as store:
            if version and store.state()['text_version'] != version:
                raise DomainError(409, 'chat_context_stale', 'Transcript changed; request the current manuscript')
            page = store.list(kind='segments', limit=200)
            if not page['items']:
                raise DomainError(409, 'transcript_empty', 'There is no speech manuscript to export')
            records = page['items']
            for offset in range(200, page['total'], 200):
                records.extend(store.list(kind='segments', offset=offset, limit=200)['items'])
    content = await run_sync(manuscript, records, format)
    return Response(content, media_type='text/plain; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="EviReel-{video_id}.{format}"'})


@router.get('/{video_id}/chat/resource')
async def chat_resource(video_id: str, request: Request,
                        kind: str = Query(pattern='^(study|transcript)$'),
                        owner: str = Query(pattern=r'^v_[0-9a-f]{32}$'),
                        artifact_id: str | None = Query(None, pattern=r'^a_[0-9a-f]{32}$'),
                        job_id: str | None = Query(None, pattern=r'^[0-9a-f]{32}$'),
                        version: str | None = Query(None, pattern=r'^[0-9a-f]{32,64}$'),
                        offset: int = Query(0, ge=0)):
    if owner != video_id:
        raise DomainError(404, 'resource_not_found', 'Resource does not belong to this video')
    return request.app.state.conversations.actions.resource(video_id, kind=kind, artifact_id=artifact_id,
        job_id=job_id, version=version, offset=offset)
