from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Source = Literal['frame', 'event', 'summary', 'transcript']
SOURCES = ('frame', 'event', 'summary', 'transcript')


class Evidence(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    evidence_id: str
    video_id: str = Field(pattern=r'^v_[0-9a-f]{32}$')
    index_version: str
    source: Source
    doc_id: str | None = None
    frame_id: str | None = None
    start_t: float = Field(ge=0)
    end_t: float = Field(ge=0)
    text: str = ''
    image_url: str | None = None
    related_frame_ids: list[str] = Field(default_factory=list)
    related_evidence_ids: list[str] = Field(default_factory=list)
    score: float = 0
    score_details: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def valid_record(self):
        if not (self.doc_id or self.frame_id) or self.end_t < self.start_t:
            raise ValueError('Evidence must locate a real record with a valid time interval')
        return self


def adapt_hit(hit, *, video_id, source, version, duration):
    tr = hit.get('time_range') or {}
    start = float(hit.get('start_t', tr.get('start_t', hit.get('t', 0))))
    end = float(hit.get('end_t', tr.get('end_t', start)))
    if not all(math.isfinite(t) for t in (start, end)) or start < 0 or end < start or end > duration + .001:
        raise ValueError('Evidence time is outside this video')
    text = str(hit.get('document_text') or hit.get('text') or hit.get('caption') or '')
    frame_id, doc_id = hit.get('frame_id') or None, hit.get('doc_id') or None
    record = frame_id if source == 'frame' else doc_id
    if not record or not version or source not in SOURCES:
        raise ValueError('Evidence has no source identity')
    identity = [video_id, source, version, record, start, end, text]
    eid = 'ev_' + hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()[:32]
    return Evidence(evidence_id=eid, video_id=video_id, index_version=version, source=source,
                    doc_id=doc_id, frame_id=frame_id, start_t=start, end_t=end, text=text,
                    image_url=f'/api/videos/{video_id}/frames/{frame_id}/image?index_version={version}' if frame_id and source != 'transcript' else None,
                    related_frame_ids=list(dict.fromkeys(hit.get('frame_ids') or ([frame_id] if frame_id else []))),
                    score=float(hit.get('score', 0)), score_details={'similarity': float(hit.get('score', 0))}).model_dump()


def legacy_hit(evidence, *, image=''):
    return dict(evidence, t=evidence['start_t'], caption=evidence['text'], image=image, absolute_t=None,
                frame_ids=evidence['related_frame_ids'], kind=evidence['source'],
                time_range={'start_t': evidence['start_t'], 'end_t': evidence['end_t'],
                            'absolute_start_t': None, 'absolute_end_t': None})


class Claim(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)


class GroundedAnswer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    claims: list[Claim] = Field(min_length=1, max_length=12)
    warnings: list[Literal['speech_visual_conflict', 'possible_asr_error', 'insufficient_evidence']] = Field(default_factory=list, max_length=3)


def validate_answer(payload, evidence):
    answer = GroundedAnswer.model_validate(payload)
    available = {e['evidence_id']: Evidence.model_validate(e).model_dump() for e in evidence}
    if len({e['video_id'] for e in available.values()}) > 1:
        raise ValueError('Mixed-video evidence is not allowed')
    citations, lines = {}, []
    for claim in answer.claims:
        # URLs, IDs and time labels must be generated from verified records, not LLM text.
        if re.search(r'ev_|https?://|file://|/api/|javascript:|data:|\]\(|(?<!\d)\d+:\d{2}|(?<![\d.])\d+(?:\.\d+)?\s*(?:s\b|秒|分钟|小时|seconds?\b|minutes?\b|hours?\b)', claim.text, re.I):
            raise ValueError('Model inserted a resource or time reference into claim text')
        refs = []
        for eid in dict.fromkeys(claim.evidence_ids):
            if eid not in available:
                raise ValueError('Citation does not belong to this retrieval')
            e = available[eid]
            citations[eid] = {k: e[k] for k in ('evidence_id', 'video_id', 'source', 'index_version', 'start_t', 'end_t')}
            refs.append(f"[{eid}] ({e['start_t']:.2f}–{e['end_t']:.2f}s)")
        lines.append(claim.text.strip() + ' ' + ' '.join(refs))
    return '\n'.join(lines), list(citations.values()), answer.warnings
