"""Question set format (JSON Lines, one question per line)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal['transcript', 'visual', 'cross_segment', 'no_answer', 'follow_up']


class Reference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start_t: float = Field(ge=0)
    end_t: float = Field(ge=0)
    source: Literal['transcript', 'frame', 'event', 'any'] = 'any'   # where the fact is: speech, screen, either
    note: str = ''

    @model_validator(mode='after')
    def ordered(self):
        if self.end_t < self.start_t:
            raise ValueError('reference end before start')
        return self


class Label(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: Literal['human', 'synthetic', 'model']   # a model-made label is never reported as a human one
    author: str = ''
    checked_against: str = ''                          # e.g. "fixture text", "video 03:10-03:40 rewatched"


class Turn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['user', 'assistant']
    content: str


class Question(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    split: Literal['tune', 'holdout']
    category: Category
    tags: list[str] = Field(default_factory=list)     # e.g. zh_term, code_symbol
    video: str                                         # fixture key (simulator) or video_id (real)
    question: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default_factory=list)  # follow-up questions: the earlier turns
    answerable: bool
    references: list[Reference] = Field(default_factory=list)
    answer_keywords: list[str] = Field(default_factory=list)   # automatic proxy only, never the gold answer
    label: Label

    @model_validator(mode='after')
    def consistent(self):
        if self.answerable and not self.references:
            raise ValueError(f'{self.id}: an answerable question needs references')
        if not self.answerable and self.references:
            raise ValueError(f'{self.id}: a no-answer question has no references')
        if (self.category == 'no_answer') == self.answerable:
            raise ValueError(f'{self.id}: category no_answer <=> answerable false')
        if self.category == 'follow_up' and not self.history:
            raise ValueError(f'{self.id}: a follow-up needs history')
        return self


def load(path, *, split=None) -> list[Question]:
    questions = []
    for number, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), start=1):
        if line.strip() and not line.lstrip().startswith('//'):
            try:
                questions.append(Question.model_validate_json(line))
            except Exception as exc:
                raise ValueError(f'{path}:{number}: {exc}') from exc
    ids = [q.id for q in questions]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate question ids')
    return [q for q in questions if split in (None, 'all') or q.split == split]


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def describe(questions) -> dict:
    out = {'questions': len(questions), 'by_split': {}, 'by_category': {}, 'by_label_source': {}, 'by_tag': {}}
    for q in questions:
        for key, value in (('by_split', q.split), ('by_category', q.category), ('by_label_source', q.label.source)):
            out[key][value] = out[key].get(value, 0) + 1
        for tag in q.tags:
            out['by_tag'][tag] = out['by_tag'].get(tag, 0) + 1
    return out


def to_json(questions) -> str:
    return '\n'.join(json.dumps(q.model_dump(), ensure_ascii=False) for q in questions) + '\n'
