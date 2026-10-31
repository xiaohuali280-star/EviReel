"""MODEL SIMULATOR for offline evaluation runs. Deterministic; NOT a model; its scores say nothing about real
answer quality. What it is for: exercising the real retrieval, fusion, routing, versions and ModelGateway
accounting end to end, so the evaluation tool itself is verified before paid runs.

* ``LexicalBackend`` -- "embeddings" = hashed bags of keyword tokens (vam.retrieval.keywords) and single
  characters; a frame's image carries its caption in a PNG text chunk, which is what gets "embedded".
* ``SimReranker``   -- cosine of the same lexical vectors.
* ``SimInspector``  -- a "VLM" that reads the frames' captions back.
* ``SimPlanner``    -- a two-turn agent planner (search + inspect, then answer).
* ``SimAnswerClient`` -- an "LLM" that cites the evidence sharing the most tokens with the question and flags
  insufficient evidence below an overlap threshold.

Every call goes through ModelGateway (admission, ledger rows, token counts) with a fixed simulated latency,
exactly where the real providers would be called.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import time

import numpy as np

from vam.retrieval.keywords import tokenize

PROVIDER = 'simulator'


def _features(text: str) -> list[str]:
    toks = tokenize(text)
    toks += [c for c in (text or '') if '一' <= c <= '鿿']
    return toks


def lexical_vector(text: str, dim: int) -> list[float]:
    vec = np.zeros(dim, dtype=np.float64)
    for tok in _features(text):
        h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:8], 'little')
        vec[h % dim] += 1.0 if (h >> 63) == 0 else -1.0
    norm = np.linalg.norm(vec)
    if norm == 0:
        vec[0], norm = 1e-6, 1e-6
    return (vec / norm).tolist()


def caption_png(caption: str) -> str:
    """A small PNG whose text chunk holds the caption: the frame payload the simulator 'sees'."""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    meta = PngInfo()
    meta.add_text('caption', caption)
    buffer = io.BytesIO()
    Image.new('RGB', (32, 18), (40, 40, 40)).save(buffer, 'PNG', pnginfo=meta)
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()


def read_caption(data_uri: str) -> str:
    from PIL import Image
    raw = base64.b64decode((data_uri or '').split(',', 1)[-1])
    try:
        return Image.open(io.BytesIO(raw)).text.get('caption', '')
    except Exception:
        return ''


def _estimate(text) -> int:
    from vam.gateway import estimate_tokens
    return estimate_tokens(len(text))


class LexicalBackend:
    name = 'e4-lexical-simulator'

    def __init__(self, dim=512, latency_s=0.04):
        self.dim, self.latency_s = dim, latency_s

    def fingerprint(self):
        return {'backend': self.name, 'dim': self.dim, 'encoding_version': 1}

    def _vector(self, text):
        return lexical_vector(text, self.dim)

    def _embed(self, texts):
        from vam.gateway import get_gateway
        joined = ''.join(texts)

        def attempt(timeout):
            time.sleep(self.latency_s)
            return [self._vector(t) for t in texts], {'input_tokens': _estimate(joined), 'output_tokens': 0}
        return get_gateway().run_sync(operation='embedding', model='sim-embedding', provider=PROVIDER,
                                      attempt_fn=attempt, est_input_tokens=_estimate(joined))

    def embed_text(self, text, *, instruction):
        return self._embed([text])[0], self.dim

    def embed_texts_batch(self, texts, *, instruction, batch_size):
        return self._embed(list(texts)), self.dim

    def embed_image_base64(self, image, *, instruction):
        return self._embed([read_caption(image)])[0], self.dim

    def embed_images_base64_batch(self, images, *, instruction, batch_size):
        return self._embed([read_caption(i) for i in images]), self.dim


class SimReranker:
    model = 'sim-rerank'

    def __init__(self, dim=512, latency_s=0.15):
        self.dim, self.latency_s = dim, latency_s

    async def rank(self, query, documents):
        from vam.gateway import get_gateway
        q = np.asarray(lexical_vector(query, self.dim))

        async def attempt():
            await asyncio.sleep(self.latency_s)
            scores = [float(q @ np.asarray(lexical_vector(d, self.dim))) for d in documents]
            ranks = sorted(enumerate(scores), key=lambda x: (-x[1], x[0]))
            text = query + ''.join(documents)
            return ranks, {'input_tokens': _estimate(text), 'output_tokens': 0}
        return await get_gateway().run(operation='rerank', model=self.model, provider=PROVIDER, attempt_fn=attempt,
                                       est_input_tokens=_estimate(query + ''.join(documents)), attempts=1)


class SimInspector:
    model = 'sim-vlm'

    def __init__(self, latency_s=0.8, tokens_per_image=256):
        self.latency_s, self.tokens_per_image = latency_s, tokens_per_image

    async def __call__(self, frames, prompt):
        from vam.gateway import get_gateway
        est = _estimate(prompt) + self.tokens_per_image * len(frames)

        async def attempt():
            await asyncio.sleep(self.latency_s)
            lines = [f'画面 {i + 1}：{read_caption(f.image_data_uri)}' for i, f in enumerate(frames)]
            text = '\n'.join(lines)
            return text, {'input_tokens': est, 'output_tokens': _estimate(text)}
        return await get_gateway().run(operation='chat', model=self.model, provider=PROVIDER, attempt_fn=attempt,
                                       est_input_tokens=est, est_output_tokens=200)


class SimAnswerClient:
    model = 'sim-llm'

    def __init__(self, latency_s=0.4, min_overlap=0.34):
        self.latency_s, self.min_overlap = latency_s, min_overlap

    async def chat(self, messages, temperature=0, max_tokens=1600):
        from vam.gateway import get_gateway
        payload = json.loads(messages[-1]['content'])
        prompt = ''.join(m['content'] for m in messages)

        async def attempt():
            await asyncio.sleep(self.latency_s)
            text = json.dumps(self.decide(payload), ensure_ascii=False)
            return text, {'input_tokens': _estimate(prompt), 'output_tokens': _estimate(text)}
        text = await get_gateway().run(operation='chat', model=self.model, provider=PROVIDER, attempt_fn=attempt,
                                       est_input_tokens=_estimate(prompt), est_output_tokens=300)
        return text, None

    def decide(self, payload):
        wanted = set(tokenize(payload['question']))
        seen = {}
        for check in payload.get('visual_checks') or []:   # what the "VLM" read counts for the frames it saw
            for eid in check.get('evidence_ids') or []:
                seen[eid] = seen.get(eid, '') + ' ' + check.get('text', '')
        scored = []
        for e in payload['evidence']:
            have = set(tokenize(e['text'] + seen.get(e['evidence_id'], '')))
            overlap = len(wanted & have) / len(wanted) if wanted else 0.0
            scored.append((overlap, e['evidence_id'], sorted(wanted & have)))
        scored.sort(key=lambda x: -x[0])
        best = [s for s in scored if s[0] >= self.min_overlap][:2]
        if not best:
            top = scored[:1]
            return {'claims': [{'text': '现有证据不足以确定答案。', 'evidence_ids': [top[0][1]]}] if top else [],
                    'warnings': ['insufficient_evidence']}
        words = sorted({w for s in best for w in s[2]})
        text = '相关内容：' + '、'.join(words[:10])
        return {'claims': [{'text': text, 'evidence_ids': [s[1] for s in best]}], 'warnings': []}


class SimPlanner:
    """A "planner" for the multi-turn agent loop (vam.retrieval.agent_search): turn 1 searches the question and
    inspects the top results, turn 2 answers (the product then replaces the draft with the grounded answer).
    One instance per question; calls go through ModelGateway like the real planner's."""
    model = 'sim-planner'

    def __init__(self, goal, latency_s=0.3, inspect_k=2):
        self.goal, self.latency_s, self.inspect_k, self.turn = goal, latency_s, inspect_k, 0

    async def chat(self, messages, temperature=0, max_tokens=500):
        from vam.gateway import get_gateway
        self.turn += 1
        plan = ({'action': 'search', 'thought': 'sim: search the question',
                 'queries': [{'q': self.goal, 'top_k': 5, 'inspect_k': self.inspect_k}]} if self.turn == 1 else
                {'action': 'answer', 'thought': 'sim: answer from what was found', 'response': 'draft'})
        prompt = ''.join(m['content'] for m in messages)

        async def attempt():
            await asyncio.sleep(self.latency_s)
            text = json.dumps(plan, ensure_ascii=False)
            return text, {'input_tokens': _estimate(prompt), 'output_tokens': _estimate(text)}
        text = await get_gateway().run(operation='chat', model=self.model, provider=PROVIDER, attempt_fn=attempt,
                                       est_input_tokens=_estimate(prompt), est_output_tokens=120)
        return text, None
