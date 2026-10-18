from typing import Literal, Annotated

from fastapi import APIRouter, Request
from pydantic import Field, StringConstraints

from vam.retrieval.evidence import Source
from vam.server.api.memory import VisualSearchRequest

router = APIRouter(prefix='/api/videos', tags=['Multimodal Search'])


class SearchRequest(VisualSearchRequest):
    sources: list[Source] | None = Field(None, min_length=1, max_length=4)
    queries: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]] = Field(default_factory=list, max_length=3)
    fusion: Literal['rrf', 'legacy'] | None = None
    rerank: bool | None = None
    answer: bool = False


@router.post('/{video_id}/search')
async def search(video_id: str, body: SearchRequest, request: Request):
    async with request.app.state.indexing.retrieval_context(video_id) as context:
        context.strategy = body.fusion or context.strategy
        context.use_rerank = context.use_rerank if body.rerank is None else body.rerank
        queries = list(dict.fromkeys(q.strip() for q in [body.query] + body.queries if q.strip()))
        for query in queries:
            await context.search(query, top_k=body.top_k, sources=body.sources,
                                 min_time=body.min_time, max_time=body.max_time if body.max_time is not None else float('inf'))
        evidence = await context.prepare(body.query, top_k=body.top_k)
        if body.answer:
            return await context.answer(body.query)
        return {'video_id': video_id, 'index_version': context.versions['visual'], 'evidence': evidence,
                'results': evidence, 'warnings': context.warnings,
                'execution': {'versions': context.versions, 'searches': context.executions}}
