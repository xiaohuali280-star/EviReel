from __future__ import annotations

import asyncio
import math
import time
from copy import deepcopy
from typing import Protocol

import httpx


class Reranker(Protocol):
    model: str
    async def rank(self, query: str, documents: list[str]) -> list[tuple[int, float]]: ...


class CohereReranker:
    """Cohere v2 contract; endpoint and credential are exclusively server settings."""
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport
        self.model = settings.rerank_model

    async def rank(self, query, documents):
        if not self.settings.rerank_api_key:
            raise RuntimeError('Rerank credential is not configured')
        async with httpx.AsyncClient(timeout=self.settings.rerank_timeout_s, transport=self.transport, follow_redirects=False) as client:
            response = await client.post('https://api.cohere.com/v2/rerank',
                                         headers={'Authorization': 'Bearer ' + self.settings.rerank_api_key},
                                         json={'model': self.model, 'query': query, 'documents': documents, 'top_n': len(documents)})
            response.raise_for_status()
            return validate_ranks(response.json().get('results'), len(documents))


def validate_ranks(results, count):
    if not isinstance(results, list) or len(results) != count:
        raise ValueError('Reranker returned an incomplete result')
    ranks, seen = [], set()
    for item in results:
        idx, score = item['index'], item['relevance_score']
        if type(idx) is not int or idx < 0 or idx >= count or idx in seen or isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError('Reranker returned invalid indices/scores')
        ranks.append((idx, float(score)))
        seen.add(idx)
    return sorted(ranks, key=lambda item: (-item[1], item[0]))


class MockReranker:
    model = 'explicit-mock-reranker'
    def __init__(self, scores):
        self.scores, self.calls = scores, []

    async def rank(self, query, documents):
        self.calls.append((query, documents))
        return validate_ranks([{'index': i, 'relevance_score': s} for i, s in enumerate(self.scores)], len(documents))


async def rerank_text(evidence, *, query, reranker, limit, timeout, text_limit):
    started = time.monotonic()
    slots = [i for i, e in enumerate(evidence[:limit]) if e['text'].strip()]
    diagnostics = {'enabled': True, 'model': reranker.model, 'covered': len(slots),
                   'uncovered': len(evidence)-len(slots), 'policy': 'text-slots-only; unranked frames retain fused slots'}
    try:
        if not slots:
            diagnostics['status'] = 'no_text_candidates'
            return evidence, diagnostics
        documents = [evidence[i]['text'][:text_limit] for i in slots]
        ranks = await asyncio.wait_for(reranker.rank(query, documents), timeout=timeout)
        # Validate injected implementations as well as the real HTTP adapter.
        ranks = validate_ranks([{'index': i, 'relevance_score': s} for i, s in ranks], len(slots))
        result = deepcopy(evidence)
        ordered = []
        for idx, score in ranks:
            e = dict(evidence[slots[idx]])
            e['score_details'] = dict(e['score_details'], rerank={'model': reranker.model, 'score': score})
            ordered.append(e)
        for slot, e in zip(slots, ordered):
            result[slot] = e
        for i, e in enumerate(result):
            e['score_details'] = dict(e['score_details'], fused_score=e['score'], final_rank=i+1)
            e['score'] = 1/(i+1)
        diagnostics['status'] = 'applied'
        return result, diagnostics
    finally:
        diagnostics['duration_s'] = time.monotonic()-started
