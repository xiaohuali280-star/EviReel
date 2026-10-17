from __future__ import annotations

from fastapi import APIRouter, Request, Query

from vam.memory.transcript import embedding_fingerprint
from vam.runtime import run_sync
from vam.server.api.memory import VisualSearchRequest
from vam.video_registry import DomainError
from vam.vision.backends import get_backend, validate_embeddings

router = APIRouter(prefix='/api/videos', tags=['Transcripts'])


@router.get('/{video_id}/transcripts')
async def list_transcripts(video_id: str, request: Request, kind: str = Query('chunks', pattern='^(segments|chunks)$'),
                           offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                           min_time: float = Query(0, ge=0, allow_inf_nan=False),
                           max_time: float | None = Query(None, ge=0, allow_inf_nan=False)):
    if max_time is not None and max_time < min_time:
        raise DomainError(422, 'invalid_time_range', 'max_time must be >= min_time')
    async with request.app.state.indexing.transcript_context(video_id) as store:
        return await run_sync(store.list, kind=kind, offset=offset, limit=limit, min_time=min_time, max_time=max_time)


@router.post('/{video_id}/transcripts/index', status_code=202)
async def index_transcripts(video_id: str, request: Request):
    return {'video_id': video_id, 'job_id': request.app.state.indexing.schedule(video_id, kind='transcript')}


@router.post('/{video_id}/transcripts/search')
async def search_transcripts(video_id: str, body: VisualSearchRequest, request: Request):
    service = request.app.state.indexing
    async with service.transcript_context(video_id) as store:
        state = store.state()
        if not state['searchable']:
            raise DomainError(409, 'transcript_search_not_ready', 'Transcript semantic index is not ready')
        backend = service.backend or get_backend()
        fingerprint = embedding_fingerprint(backend)
        version = store.version(state['search_version'])
        if version['embedding_fingerprint'] != fingerprint:
            raise DomainError(409, 'incompatible_transcript_model', 'Transcript model changed; rebuild its semantic index')
        try:
            vector, dim = await run_sync(backend.embed_text, body.query.strip(), instruction='search_query')
            validate_embeddings([vector], expected_count=1, expected_dim=version['dim'])
        except Exception as exc:
            raise DomainError(502, 'query_embedding_failed', 'Transcript query embedding failed') from exc
        return await run_sync(store.search, vector, fingerprint=fingerprint, dim=dim, top_k=body.top_k,
                              min_time=body.min_time, max_time=body.max_time)
