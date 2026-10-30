"""Runs experiment configs over a question set and writes raw per-question results, metadata and summaries.

Each config fixes the retrieval sources, fusion, keyword channel, rerank and answer route; everything else
(model identities, retrieval settings, dataset hash, code version) is recorded in metadata.json. A run is
reproducible from that file: same dataset, same fixture spec, same configs, same code.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
import tempfile
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from vam.evaluation import dataset as ds
from vam.evaluation import metrics
from vam.video_registry import DomainError

# Comparable configs: one change at a time against "fusion" (the default retrieval) or against each other.
CONFIGS = {
    'transcript':     {'sources': ['transcript'], 'keyword': False, 'rerank': False, 'mode': 'cheap'},
    'fusion':         {'sources': None, 'keyword': False, 'rerank': False, 'mode': 'cheap'},
    'fusion_keyword': {'sources': None, 'keyword': True, 'rerank': False, 'mode': 'cheap'},
    'fusion_rerank':  {'sources': None, 'keyword': False, 'rerank': True, 'mode': 'cheap'},
    'deep':           {'sources': None, 'keyword': False, 'rerank': False, 'mode': 'deep'},
    'auto':           {'sources': None, 'keyword': False, 'rerank': False, 'mode': 'auto'},
    'auto_keyword':   {'sources': None, 'keyword': True, 'rerank': False, 'mode': 'auto'},
}
REAL_ONLY = {'agent'}   # the multi-turn agent needs a real tool-calling chat model; not simulated


class EvalLedger:
    """In-memory usage ledger for an evaluation run (the gateway's ledger slot): every provider attempt is a
    row tagged with the current question; ``max_calls`` / ``max_tokens`` cap a paid run (reservation
    refused once a cap would be passed -- the call is never sent)."""
    persistent = False

    def __init__(self, settings, *, max_calls=None, max_tokens=None):
        self.settings, self.max_calls, self.max_tokens = settings, max_calls, max_tokens
        self.rows, self.current = [], None

    def _used(self):
        return sum((r['input_tokens'] or r['est_in']) + (r['output_tokens'] or r['est_out']) for r in self.rows
                   if r['status'] != 'failed')

    def reserve(self, call):
        from vam.gateway.ledger import price_basis
        if self.max_calls is not None and len(self.rows) >= self.max_calls:
            raise DomainError(429, 'eval_budget_exceeded', 'Evaluation call cap reached')
        if self.max_tokens is not None and self._used() + call.est_input_tokens + call.est_output_tokens > self.max_tokens:
            raise DomainError(429, 'eval_budget_exceeded', 'Evaluation token cap reached')
        basis, prices = price_basis(self.settings, call.model)
        self.rows.append({'call_id': call.call_id, 'question': self.current, 'operation': call.operation, 'model': call.model,
                          'provider': call.provider, 'attempt': call.attempt, 'status': 'reserved',
                          'est_in': call.est_input_tokens, 'est_out': call.est_output_tokens,
                          'input_tokens': None, 'output_tokens': None, 'usage_source': None, 'prices': prices,
                          'price_basis': basis})

    def _row(self, call):
        return next(r for r in reversed(self.rows) if r['call_id'] == call.call_id)

    def settle(self, call, input_tokens, output_tokens, source):
        self._row(call).update(status='settled', input_tokens=input_tokens, output_tokens=output_tokens, usage_source=source)

    def fail(self, call, code):
        self._row(call).update(status='failed', error=code)

    def unknown(self, call, code):
        self._row(call).update(status='unknown', error=code)

    def reconcile(self):
        return 0

    def usage(self, question):
        from vam.gateway.ledger import cost_of
        rows = [r for r in self.rows if r['question'] == question]
        calls, tin, tout, cost, known = {}, 0, 0, Decimal(0), True
        for r in rows:
            calls[r['operation']] = calls.get(r['operation'], 0) + 1
            if r['status'] == 'failed':
                continue
            i = r['input_tokens'] if r['input_tokens'] is not None else r['est_in']
            o = r['output_tokens'] if r['output_tokens'] is not None else r['est_out']
            tin, tout = tin + i, tout + o
            c = cost_of(r['prices'], i, o)
            if c is None:
                known = False
            else:
                cost += c
        return {'calls': calls, 'input_tokens': tin, 'output_tokens': tout, 'cost': float(cost) if known and rows else None,
                'attempts': len(rows), 'unknown_outcomes': sum(1 for r in rows if r['status'] == 'unknown')}


def git_info(root):
    def run(*args):
        try:
            return subprocess.run(['git', *args], capture_output=True, text=True, cwd=root, timeout=10).stdout.strip()
        except Exception:
            return None
    return {'commit': run('rev-parse', 'HEAD'), 'dirty': bool(run('status', '--porcelain', 'vam'))}


def _query(q, followup):
    if q.category == 'follow_up' and followup == 'history':
        # Simulated context resolution: earlier user turns + the question (the product rewrites with an LLM).
        return ' '.join([t.content for t in q.history if t.role == 'user'] + [q.question])
    return q.question


async def _warm_up(service, video_id, config, sims, models):
    """One untimed query per config and video: opens the index readers (a cold first query would otherwise
    sit in the latency tail of whichever config runs first)."""
    async with service.retrieval_context(video_id) as ctx:
        ctx.use_keyword = config['keyword']
        await ctx.search('warm up', top_k=1, sources=config['sources'])


async def run_config(service, questions, video_ids, name, config, *, models, top_k, followup, ledger, sims, repeat=0):
    from vam.gateway import model_scope
    rows = []
    ledger.current = (name, repeat, '__warmup__')
    for video in sorted({q.video for q in questions}):
        with model_scope('evaluation', 'interactive'):
            await _warm_up(service, video_ids[video], config, sims, models)
    for q in questions:
        ledger.current = (name, repeat, q.id)
        query = _query(q, followup)
        started = time.monotonic()
        record = q.model_dump() | {'config': name, 'query_used': query}
        try:
            with model_scope('evaluation', 'interactive'):
                async with service.retrieval_context(video_ids[q.video]) as ctx:
                    ctx.strategy = 'rrf'
                    ctx.use_keyword, ctx.use_rerank = config['keyword'], config['rerank']
                    if models == 'simulator':
                        ctx.reranker, ctx.answer_client, ctx.inspector = sims['reranker'], sims['answer'], sims['inspector']
                    await ctx.search(query, top_k=top_k, sources=config['sources'])
                    evidence = await ctx.prepare(query, top_k=top_k)
                    retrieval_s = time.monotonic() - started
                    answer = await ctx.answer(query, mode=config['mode'])
            record['error'] = None
        except DomainError as exc:
            evidence, answer, retrieval_s = [], {'status': 'error', 'answer': '', 'citations': []}, time.monotonic() - started
            record['error'] = exc.code
        record['latency'] = {'total_s': time.monotonic() - started, 'retrieval_s': retrieval_s}
        record['evidence'] = [{k: e[k] for k in ('evidence_id', 'source', 'start_t', 'end_t', 'score')} | {'text': e['text'][:200]}
                              for e in evidence]
        record['answer'] = {k: answer.get(k) for k in ('status', 'answer', 'citations')}
        record['route'] = answer.get('route')
        record['warnings'] = sorted({w['code'] for w in answer.get('warnings') or []})
        record['snapshot_versions'] = answer.get('execution', {}).get('versions')
        record['scores'] = metrics.score_question(q.model_dump(), evidence, answer)
        record['usage'] = ledger.usage((name, repeat, q.id))
        rows.append(record)
    return rows


@contextmanager
def _environment(models, data_root, *, fixture, max_calls, max_tokens, overrides):
    """Settings, IndexingService, gateway (with the run ledger) and, in simulator mode, the corpus."""
    from vam import gateway as gw
    from vam.config import Settings
    from vam.evaluation import simulator
    from vam.gateway.limiter import LocalLimiter
    from vam.indexing.service import IndexingService
    temporary = None
    if models == 'simulator':
        temporary = tempfile.TemporaryDirectory(prefix='evireel-eval-')
        data_root = Path(temporary.name) / 'data'
        settings = Settings(_env_file=None, data_root=str(data_root), enable_retrieval=True, rerank_enabled=False,
                            asr_enabled=False, query_include_latest_segment_video=False, study_auto_generate=False,
                            frame_store_path=str(Path(temporary.name) / 'default.sqlite3'), siliconflow_api_key='',
                            openrouter_api_key='', dashscope_api_key='', model_prices='', **overrides)
        backend = simulator.LexicalBackend()
    else:
        settings = Settings(data_root=str(data_root), **overrides)
        backend = None
    ledger = EvalLedger(settings, max_calls=max_calls, max_tokens=max_tokens)
    gateway = gw.ModelGateway(settings, LocalLimiter(settings), ledger)
    previous = gw._gateway
    gw.configure(gateway)
    service = IndexingService(settings, backend=backend, transcriber=object())
    try:
        fixture_ids = None
        if models == 'simulator':
            from vam.evaluation.fixture import build
            fixture_ids = build(fixture, service, backend)
        yield settings, service, ledger, fixture_ids, backend
    finally:
        gw.configure(previous)
        if temporary is not None:
            temporary.cleanup()


def run(*, dataset_path, out, configs, models, fixture=None, data_root=None, split='all', top_k=5, followup='history',
        repeat=1, allow_paid=False, max_calls=None, max_tokens=None, overrides=None):
    from vam.evaluation import simulator
    questions = ds.load(dataset_path, split=split)
    if models == 'real':
        if not allow_paid or max_calls is None or max_tokens is None:
            raise SystemExit('real models cost money: pass --allow-paid with --max-calls and --max-tokens')
        if data_root is None:
            raise SystemExit('real runs read indexed videos: pass --data-root')
    elif models == 'simulator':
        if fixture is None:
            raise SystemExit('simulator runs need --fixture')
        from vam.evaluation.fixture import check_references
        problems = check_references(fixture, questions)
        if problems:
            raise SystemExit('references do not match the fixture:\n' + '\n'.join(problems))
    names = list(CONFIGS) if configs == ['all'] else configs
    unknown = [n for n in names if n not in CONFIGS and n not in REAL_ONLY]
    if unknown:
        raise SystemExit(f'unknown configs: {unknown}')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).parents[2]
    metadata = {'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'git': git_info(root),
                'environment': {'python': platform.python_version(), 'os': platform.platform(), 'cpu_count': os.cpu_count()},
                'dataset': {'path': str(dataset_path), 'sha256': ds.digest(dataset_path), 'split': split, **ds.describe(questions)},
                'fixture': {'path': str(fixture), 'sha256': ds.digest(fixture)} if fixture else None,
                'models': models, 'configs': {n: CONFIGS.get(n, {'real_only': True}) for n in names},
                'top_k': top_k, 'followup': followup, 'repeat': repeat, 'metric_tolerance_s': metrics.TOLERANCE_S,
                'caps': {'max_calls': max_calls, 'max_tokens': max_tokens}, 'not_run': {},
                'warm_up': 'one untimed query per config and video before measuring (not in results or usage)'}
    if models == 'simulator':
        metadata['model_identities'] = {'embedding': simulator.LexicalBackend().fingerprint(), 'rerank': 'sim-rerank',
                                        'vlm': 'sim-vlm', 'llm': 'sim-llm',
                                        'note': 'deterministic simulator: scores verify the tool, not answer quality'}
    summaries = {}
    with _environment(models, data_root, fixture=fixture, max_calls=max_calls, max_tokens=max_tokens,
                      overrides=overrides or {}) as (settings, service, ledger, fixture_ids, backend):
        video_ids = {k: v['video_id'] for k, v in fixture_ids.items()} if fixture_ids else {q.video: q.video for q in questions}
        metadata['videos'] = fixture_ids or video_ids
        metadata['settings'] = {k: getattr(settings, k) for k in (
            'retrieval_candidates_per_source', 'retrieval_min_similarity', 'retrieval_max_evidence', 'retrieval_rrf_k',
            'retrieval_inspect_frames', 'route_min_similarity', 'route_min_keyword_coverage', 'route_escalation_timeout_s',
            'model_id', 'rerank_model')}
        sims = {'reranker': simulator.SimReranker(), 'answer': simulator.SimAnswerClient(),
                'inspector': simulator.SimInspector()} if models == 'simulator' else {}
        for name in names:
            if name in REAL_ONLY and models != 'real':
                metadata['not_run'][name] = 'needs a real tool-calling chat model (simulator mode)'
                continue
            if name in REAL_ONLY:
                metadata['not_run'][name] = 'agent evaluation entry not implemented for the search API path'
                continue
            all_rows = []
            for r in range(repeat):
                rows = asyncio.run(run_config(service, questions, video_ids, name, CONFIGS[name], models=models,
                                              top_k=top_k, followup=followup, ledger=ledger, sims=sims, repeat=r))
                for row in rows:
                    row['repeat'] = r
                all_rows += rows
            with (out / f'{name}.results.jsonl').open('w', encoding='utf-8') as handle:
                for row in all_rows:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
            summaries[name] = {'overall': metrics.summarize(all_rows),
                               'by_split': metrics.by_group(all_rows, 'split'),
                               'by_category': metrics.by_group(all_rows, 'category'),
                               'by_tag': metrics.by_group([r for r in all_rows if r['tags']], 'tags')}
    metadata['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    (out / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    (out / 'summary.json').write_text(json.dumps(summaries, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    (out / 'comparison.md').write_text(comparison(summaries, metadata), encoding='utf-8')
    return summaries, metadata


def _fmt(v, pct=False):
    if v is None:
        return '—'
    return f'{v * 100:.0f}%' if pct else (f'{v:.3f}' if isinstance(v, float) else str(v))


def comparison(summaries, metadata, split=None) -> str:
    head = ('| config | recall@1 | recall@5 | MRR | citation support (auto) | abstain on no-answer | false abstain | '
            'time error median (s) | latency p50 / p95 (s) | model calls / question | tokens / question |\n'
            '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n')
    lines = []
    for name, s in summaries.items():
        o = s['overall'] if split is None else s['by_split'].get(split, {})
        if not o:
            continue
        calls = sum(o['calls_per_question'].values())
        tokens = o['tokens_per_question']['input'] + o['tokens_per_question']['output']
        lines.append(f"| {name} | {_fmt(o['evidence_recall@1'], True)} | {_fmt(o['evidence_recall@5'], True)} | "
                     f"{_fmt(o['mrr'])} | {_fmt(o.get('citation_support_auto'), True)} | "
                     f"{_fmt(o.get('abstention_on_unanswerable'), True)} | {_fmt(o.get('false_abstention_on_answerable'), True)} | "
                     f"{_fmt(o.get('time_error_s_median'))} | {_fmt(o['latency_total_s']['p50'])} / {_fmt(o['latency_total_s']['p95'])} | "
                     f"{calls:.1f} | {tokens:.0f} |")
    note = (f"\nmodels: **{metadata['models']}** · dataset {metadata['dataset']['questions']} questions "
            f"({metadata['dataset']['by_label_source']}) · commit {metadata['git']['commit']} · top_k {metadata['top_k']}\n")
    if metadata['models'] == 'simulator':
        note += '\nSimulator run: the numbers verify the tooling and the routing logic; they are not answer quality.\n'
    return head + '\n'.join(lines) + '\n' + note
