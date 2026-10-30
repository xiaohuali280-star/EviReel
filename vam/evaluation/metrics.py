"""Metric definitions. Each summary value says what it counts and over which denominator; a metric that
cannot be computed for a run is ``None`` with the reason, never 0.

Per question (``score_question``):
* evidence hit / recall @K -- an evidence item matches a reference when their time intervals overlap
  (with ``TOLERANCE_S`` slack on both ends) and, for a reference with a source, the item comes from that
  source (``any`` accepts every source). recall@K = matched references / references; hit@K = any matched.
* reciprocal rank -- 1 / rank of the first matching evidence item (0 when none).
* citation support -- AUTOMATIC: a citation is supported when its interval matches a reference as above.
  This checks *where* the citation points, not whether its text supports the claim; human review is a
  separate column and is reported as not performed unless a reviewer filled it in.
* time error -- seconds between the start of the first citation (else the top evidence item) and the
  nearest reference start.
* abstained -- status refused or uncertain.
* keyword proxy -- every ``answer_keywords`` entry appears in the answer (case-insensitive): a weak
  automatic proxy, reported as such.
"""
from __future__ import annotations

import math
import statistics

TOLERANCE_S = 2.0
KS = (1, 3, 5)


def matches(item, ref) -> bool:
    if ref['source'] not in ('any', item['source']):
        return False
    return item['start_t'] <= ref['end_t'] + TOLERANCE_S and item['end_t'] >= ref['start_t'] - TOLERANCE_S


def score_question(question, evidence, answer=None) -> dict:
    """``question`` = dataset.Question as dict; ``evidence`` = ranked evidence (source, start_t, end_t);
    ``answer`` = the answer result (status, answer, citations) or None for retrieval-only runs."""
    refs = question['references']
    out = {'answerable': question['answerable']}
    if question['answerable']:
        for k in KS:
            top = evidence[:k]
            covered = sum(1 for r in refs if any(matches(e, r) for e in top))
            out[f'recall@{k}'] = covered / len(refs)
            out[f'hit@{k}'] = covered > 0
        rank = next((i + 1 for i, e in enumerate(evidence) if any(matches(e, r) for r in refs)), None)
        out['reciprocal_rank'] = 1 / rank if rank else 0.0
    if answer is not None:
        status = answer.get('status')
        citations = answer.get('citations') or []
        out['status'] = status
        out['abstained'] = status in {'refused', 'uncertain'}
        out['citations'] = len(citations)
        if question['answerable']:
            out['citations_supported'] = sum(1 for c in citations if any(matches(c, r) for r in refs))
            anchor = citations[0] if citations else (evidence[0] if evidence else None)
            out['time_error_s'] = (min(abs(anchor['start_t'] - r['start_t']) for r in refs)
                                   if anchor is not None else None)
            keywords = question.get('answer_keywords') or []
            text = (answer.get('answer') or '').casefold()
            out['keyword_proxy'] = (all(k.casefold() in text for k in keywords)
                                    if keywords and status == 'answered' else None)
    return out


def _mean(values):
    values = [v for v in values if v is not None]
    return (sum(values) / len(values)) if values else None


def _pct(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[min(len(values) - 1, max(0, math.ceil(q * len(values)) - 1))]


def summarize(rows) -> dict:
    """``rows`` = per-question records (question fields + ``scores`` + ``usage`` + ``latency``)."""
    answerable = [r for r in rows if r['answerable']]
    unanswerable = [r for r in rows if not r['answerable']]
    answered_rows = [r for r in rows if 'status' in r['scores']]
    s = {'questions': len(rows), 'answerable': len(answerable), 'unanswerable': len(unanswerable)}
    for k in KS:
        s[f'evidence_recall@{k}'] = _mean([r['scores'].get(f'recall@{k}') for r in answerable])
        s[f'evidence_hit@{k}'] = _mean([float(r['scores'][f'hit@{k}']) for r in answerable]) if answerable else None
    s['mrr'] = _mean([r['scores'].get('reciprocal_rank') for r in answerable])
    s['denominator_retrieval'] = f'{len(answerable)} answerable questions'
    if answered_rows:
        cited = sum(r['scores'].get('citations', 0) for r in answered_rows if r['answerable'])
        supported = sum(r['scores'].get('citations_supported', 0) for r in answered_rows if r['answerable'])
        s['citation_support_auto'] = supported / cited if cited else None
        s['denominator_citations'] = f'{cited} citations on answerable questions'
        s['citation_support_human'] = None
        s['citation_support_human_reason'] = 'not reviewed by a person in this run'
        times = [r['scores'].get('time_error_s') for r in answered_rows if r['answerable']]
        s['time_error_s_median'] = statistics.median([t for t in times if t is not None]) if any(t is not None for t in times) else None
        s['time_error_s_mean'] = _mean(times)
        s['abstention_on_unanswerable'] = _mean([float(r['scores']['abstained']) for r in answered_rows if not r['answerable']]) \
            if unanswerable else None
        s['denominator_abstention'] = f'{len(unanswerable)} no-answer questions'
        s['false_abstention_on_answerable'] = _mean([float(r['scores']['abstained']) for r in answered_rows if r['answerable']])
        s['keyword_proxy'] = _mean([float(v) for v in (r['scores'].get('keyword_proxy') for r in answered_rows) if v is not None])
        s['keyword_proxy_note'] = 'automatic: all answer_keywords appear in answered answers; not a quality judgment'
        statuses = {}
        for r in answered_rows:
            statuses[r['scores']['status']] = statuses.get(r['scores']['status'], 0) + 1
        s['statuses'] = statuses
        routes = {}
        for r in answered_rows:
            path = (r.get('route') or {}).get('path')
            routes[path] = routes.get(path, 0) + 1
        s['routes'] = routes
    latency = [r['latency']['total_s'] for r in rows]
    retrieval = [r['latency']['retrieval_s'] for r in rows]
    s['latency_total_s'] = {'p50': _pct(latency, .5), 'p95': _pct(latency, .95), 'mean': _mean(latency)}
    s['latency_retrieval_s'] = {'p50': _pct(retrieval, .5), 'p95': _pct(retrieval, .95)}
    calls, tokens = {}, {'input': 0, 'output': 0}
    costs, cost_known = [], True
    for r in rows:
        for op, n in r['usage']['calls'].items():
            calls[op] = calls.get(op, 0) + n
        tokens['input'] += r['usage']['input_tokens']
        tokens['output'] += r['usage']['output_tokens']
        if r['usage']['cost'] is None:
            cost_known = False
        else:
            costs.append(r['usage']['cost'])
    n = max(1, len(rows))
    s['calls_per_question'] = {op: c / n for op, c in sorted(calls.items())}
    s['tokens_per_question'] = {'input': tokens['input'] / n, 'output': tokens['output'] / n}
    s['cost_per_question'] = (sum(costs) / n) if cost_known and costs else None
    if not cost_known or not costs:
        s['cost_reason'] = 'no price basis configured for these models (MODEL_PRICES); tokens are reported instead'
    return s


def by_group(rows, key) -> dict:
    groups = {}
    for r in rows:
        values = r[key] if isinstance(r[key], list) else [r[key]]
        for value in values:
            groups.setdefault(value, []).append(r)
    return {g: summarize(items) for g, items in sorted(groups.items())}
