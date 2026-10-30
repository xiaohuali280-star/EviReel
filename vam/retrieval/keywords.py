"""E4 keyword channel: exact-term recall next to the embedding channels.

Embeddings are weak at exact matches: a Chinese technical term split differently by the ASR, or a code
symbol such as ``union_find.find()`` / ``std::vector`` / ``__init__``. This channel scores transcript chunks
with BM25 over two kinds of tokens:

* code-like symbols: a dotted / ``::`` / ``->`` path is kept whole (``std::vector``) and also split into its
  parts (``std``, ``vector``), so a question naming either form matches;
* CJK text: character bigrams of every CJK run (single characters only for one-character runs), the usual
  dictionary-free tokenisation for Chinese retrieval;
* other words: lower-cased ASCII words and numbers.

It is a candidate source fused by RRF like the others, off by default (``RETRIEVAL_KEYWORD``); whether it
helps is decided by the evaluation (docs/engineering/EVALUATION.md), not assumed. No index is persisted: a
video holds at most a few thousand chunks, and the pinned transcript version is scored per request.
"""
from __future__ import annotations

import math
import re
from collections import Counter

SYMBOL = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:(?:::|\.|->)[A-Za-z_][A-Za-z0-9_]*)*')
CJK_RUN = re.compile(r'[㐀-䶿一-鿿豈-﫿]+')
NUMBER = re.compile(r'\d+(?:\.\d+)?')


def tokenize(text: str) -> list[str]:
    tokens = []
    for match in SYMBOL.finditer(text or ''):
        symbol = match.group(0).lower()
        parts = re.split(r'::|\.|->', symbol)
        if len(parts) > 1:
            tokens.append(symbol)
        tokens.extend(p for p in parts if p)
    for run in CJK_RUN.findall(text or ''):
        tokens.extend([run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)])
    tokens.extend(NUMBER.findall(text or ''))
    return tokens


def bm25(query: str, documents: list[str], *, k1: float = 1.2, b: float = 0.75) -> list[float]:
    """BM25 score of each document for the query (0 where no query token occurs)."""
    terms = Counter(tokenize(query))
    docs = [Counter(tokenize(d)) for d in documents]
    if not terms or not docs:
        return [0.0] * len(documents)
    average = sum(sum(d.values()) for d in docs) / len(docs) or 1.0
    df = Counter(t for d in docs for t in d)
    scores = []
    for d in docs:
        length, score = sum(d.values()), 0.0
        for term in terms:
            tf = d.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (len(docs) - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / average))
        scores.append(score)
    return scores
