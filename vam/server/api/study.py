from fastapi import APIRouter, Query, Request, Response
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


def _pipeline(request):
    pipeline = getattr(request.app.state, 'pipeline', None)
    if pipeline is None:
        raise DomainError(404, 'pipeline_disabled', 'Export jobs require PIPELINE_MODE=worker; use the direct export')
    return pipeline


@router.post('/{video_id}/study/artifacts/{artifact_id}/exports', status_code=202)
async def create_export(video_id: str, artifact_id: str, request: Request,
                        format: str = Query('md', pattern='^(md|pptx|pdf)$')):
    """Worker mode: render the export in a pipeline worker into object storage (durable, retryable)."""
    from vam.server.api.videos import idempotency_key
    request.app.state.study.artifact(video_id, artifact_id)   # exists and belongs to this (authorized) video
    job_id = await run_sync(_pipeline(request).export, video_id, artifact_id, format, idempotency_key(request))
    return {'video_id': video_id, 'artifact_id': artifact_id, 'format': format, 'job_id': job_id}


@router.get('/{video_id}/study/exports/{job_id}/download')
async def download_export(video_id: str, job_id: str, request: Request):
    data, media, filename = await run_sync(_pipeline(request).export_download, video_id, job_id,
                                           request.app.state.platform.store)
    return Response(data, media_type=media, headers={'Content-Disposition': f'attachment; filename="{filename}"'})


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
    async with service.transcript_context(video_id) as store:
        current = store.state()['text_version']
        if version and current != version:
            raise DomainError(409, 'chat_context_stale', 'Transcript changed; request the current manuscript')
        # Every page at the version read first (E3): a publication between pages cannot mix two versions.
        page = store.list(kind='segments', limit=200, version=current)
        if not page['items']:
            raise DomainError(409, 'transcript_empty', 'There is no speech manuscript to export')
        records = page['items']
        for offset in range(200, page['total'], 200):
            records.extend(store.list(kind='segments', offset=offset, limit=200, version=current)['items'])
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
