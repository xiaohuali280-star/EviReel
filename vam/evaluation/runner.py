"""Runs experiment configs over a question set and writes raw per-question results, metadata and summaries.

Each config fixes the retrieval sources, fusion, keyword channel, rerank and answer route (or the multi-turn
agent loop); everything else (model identities, retrieval settings, dataset hash, code version, the platform
wiring) is recorded in metadata.json. A run is reproducible from that file: same dataset, same fixture spec,
same configs, same code.

Real runs use the API's own platform wiring (vam.server.assembly): PostgreSQL read leases, the shared Redis
rate windows, the persistent usage ledger with the workspace budgets, the owning workspace of each video and
object restore. The evaluation ledger only adds per-question attribution and the run's call/token caps on top
of that ledger; it never replaces it. Simulator runs stay isolated (temporary DATA_ROOT, no middleware).
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
    # The product's multi-turn tool-calling loop (vam.retrieval.agent_search): planner turns choose searches and
    # inspections within RETRIEVAL_MAX_TURNS, candidates per channel and RETRIEVAL_INSPECT_FRAMES, under the
    # per-question deadline CONVERSATION_REQUEST_TIMEOUT_S; the final answer is the grounded deep answer.
    'agent':          {'sources': None, 'keyword': False, 'rerank': False, 'mode': 'deep', 'agent': True},
}


def billed_tokens(row):
    """Tokens a usage row counts for: provider-reported when present (0 is a valid report), else the estimate."""
    i = row['input_tokens'] if row['input_tokens'] is not None else row['est_in']
    o = row['output_tokens'] if row['output_tokens'] is not None else row['est_out']
    return i, o


class EvalLedger:
    """The gateway's ledger slot during an evaluation run: every provider attempt is a row tagged with the
    current (config, repeat, question); ``max_calls`` / ``max_tokens`` cap the run (reservation refused once a
    cap would be passed -- the call is never sent).

    ``inner`` is the platform's own ledger (PgLedger in platform mode): every reservation, settlement, failure
    and unknown outcome goes there first, so the workspace budget, the persistent record and the reconciler see
    evaluation calls exactly like API calls. The run caps are checked before the inner reservation; a refusal by
    the inner ledger (workspace budget) is raised unchanged and no evaluation row is written."""

    def __init__(self, settings, *, inner=None, max_calls=None, max_tokens=None):
        self.settings, self.inner, self.max_calls, self.max_tokens = settings, inner, max_calls, max_tokens
        self.rows, self.current = [], None

    @property
    def persistent(self):
        return bool(getattr(self.inner, 'persistent', False))

    def _used(self):
        return sum(sum(billed_tokens(r)) for r in self.rows if r['status'] != 'failed')

    def reserve(self, call):
        from vam.gateway.ledger import price_basis
        if self.max_calls is not None and len(self.rows) >= self.max_calls:
            raise DomainError(429, 'eval_budget_exceeded', 'Evaluation call cap reached')
        if self.max_tokens is not None and self._used() + call.est_input_tokens + call.est_output_tokens > self.max_tokens:
            raise DomainError(429, 'eval_budget_exceeded', 'Evaluation token cap reached')
        if self.inner is not None:
            self.inner.reserve(call)
        basis, prices = price_basis(self.settings, call.model)
        self.rows.append({'call_id': call.call_id, 'question': self.current, 'operation': call.operation, 'model': call.model,
                          'provider': call.provider, 'workspace': call.workspace, 'attempt': call.attempt,
                          'status': 'reserved', 'est_in': call.est_input_tokens, 'est_out': call.est_output_tokens,
                          'input_tokens': None, 'output_tokens': None, 'usage_source': None, 'prices': prices,
                          'price_basis': basis})

    def _row(self, call):
        return next((r for r in reversed(self.rows) if r['call_id'] == call.call_id), None)

    def _update(self, call, **fields):
        row = self._row(call)
        if row is not None:
            row.update(fields)

    def settle(self, call, input_tokens, output_tokens, source):
        if self.inner is not None:
            self.inner.settle(call, input_tokens, output_tokens, source)
        self._update(call, status='settled', input_tokens=input_tokens, output_tokens=output_tokens, usage_source=source)

    def fail(self, call, code):
        if self.inner is not None:
            self.inner.fail(call, code)
        self._update(call, status='failed', error=code)

    def unknown(self, call, code):
        if self.inner is not None:
            self.inner.unknown(call, code)
        self._update(call, status='unknown', error=code)

    def reconcile(self):
        return self.inner.reconcile() if self.inner is not None else 0

    def observed_models(self):
        """(operation, provider, model) the gateway actually saw, with the price basis each was charged on."""
        seen = {}
        for r in self.rows:
            seen.setdefault((r['operation'], r['provider'], r['model']), r['price_basis'])
        return [{'operation': o, 'provider': p, 'model': m, 'price_basis': b} for (o, p, m), b in sorted(seen.items())]

    def usage(self, question):
        from vam.gateway.ledger import cost_of
        rows = [r for r in self.rows if r['question'] == question]
        calls, tin, tout, cost, known = {}, 0, 0, Decimal(0), True
        for r in rows:
            calls[r['operation']] = calls.get(r['operation'], 0) + 1
            if r['status'] == 'failed':
                continue
            i, o = billed_tokens(r)
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


class AgentRunError(Exception):
    def __init__(self, code, trace):
        super().__init__(code)
        self.code, self.trace = code, trace


async def _agent_answer(ctx, query, history, settings, planner):
    """One question through the product's multi-turn agent loop, bounded by the per-question deadline.
    Returns (evidence, answer, trace); a timeout, a planner/tool exception or a loop that never produced a
    final result raises AgentRunError (recorded as the question's error, never as an answer)."""
    from vam.retrieval.agent_search import search_generator
    from vam.retrieval.frame_store import FrameStore
    if planner is not None:
        ctx.planner_client = planner
    store = ctx.visual if ctx.visual is not None else FrameStore(backend=ctx.backend)
    trace = {'planner_turns': 0, 'searches': 0, 'max_candidates': 0, 'inspections': 0, 'loop_errors': [],
             'max_turns': settings.retrieval_max_turns, 'deadline_s': settings.conversation_request_timeout_s}
    final = {}

    async def drive():
        async for event in search_generator(query, store_instance=store, chat_history=history, retrieval_context=ctx):
            kind = event.get('type')
            if kind == 'plan':
                trace['planner_turns'] += 1
            elif kind == 'search_done':
                trace['searches'] += 1
                trace['max_candidates'] = max(trace['max_candidates'], int(event.get('hits') or 0))
            elif kind == 'inspection_start':
                trace['inspections'] += 1
            elif kind == 'error':
                trace['loop_errors'].append(str(event.get('message'))[:200])
            elif kind == 'final':
                final['result'] = event.get('result')
    try:
        await asyncio.wait_for(drive(), settings.conversation_request_timeout_s)
    except (TimeoutError, asyncio.TimeoutError):
        raise AgentRunError('agent_timeout', trace) from None
    except DomainError as exc:
        raise AgentRunError(exc.code, trace) from None
    except Exception as exc:
        trace['exception'] = type(exc).__name__
        raise AgentRunError('agent_failed', trace) from None
    if not isinstance(final.get('result'), dict):
        raise AgentRunError('agent_no_final', trace)
    return ctx.evidence(), final['result'], trace


async def run_config(service, questions, video_ids, name, config, *, models, top_k, followup, ledger, sims, repeat=0):
    from vam.gateway import model_scope
    rows = []

    def workspace(video_id):
        # The owning workspace (platform): budget, rate window and cache scope of the API's own questions.
        return service.workspace_of(video_id) if service.workspace_of is not None else 'evaluation'
    ledger.current = (name, repeat, '__warmup__')
    for video in sorted({q.video for q in questions}):
        with model_scope(workspace(video_ids[video]), 'interactive'):
            await _warm_up(service, video_ids[video], config, sims, models)
    for q in questions:
        ledger.current = (name, repeat, q.id)
        query = _query(q, followup)
        started = time.monotonic()
        record = q.model_dump() | {'config': name, 'query_used': query}
        retrieval_s = None
        try:
            with model_scope(workspace(video_ids[q.video]), 'interactive'):
                async with service.retrieval_context(video_ids[q.video]) as ctx:
                    ctx.strategy = 'rrf'
                    ctx.use_keyword, ctx.use_rerank = config['keyword'], config['rerank']
                    ctx.answer_mode = config['mode']
                    if models == 'simulator':
                        ctx.reranker, ctx.answer_client, ctx.inspector = sims['reranker'], sims['answer'], sims['inspector']
                    if config.get('agent'):
                        planner = sims['planner'](query) if models == 'simulator' else None
                        history = [t.model_dump() for t in q.history]
                        evidence, answer, record['agent'] = await _agent_answer(ctx, query, history, service.settings, planner)
                    else:
                        await ctx.search(query, top_k=top_k, sources=config['sources'])
                        evidence = await ctx.prepare(query, top_k=top_k)
                        retrieval_s = time.monotonic() - started
                        answer = await ctx.answer(query, mode=config['mode'])
            record['error'] = None
        except (DomainError, AgentRunError) as exc:
            evidence, answer = [], {'status': 'error', 'answer': '', 'citations': []}
            record['error'] = exc.code
            if isinstance(exc, AgentRunError):
                record['agent'] = exc.trace
        if retrieval_s is None:
            retrieval_s = time.monotonic() - started   # the agent interleaves retrieval and answering
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
def _environment(models, data_root, *, fixture, max_calls, max_tokens, overrides, standalone=False):
    """Settings, IndexingService, gateway (with the run ledger) and, in simulator mode, the corpus.

    Real mode installs its Settings as the process settings for the run, so the providers (which read
    get_settings()), the gateway, the index readers and the recorded identities all use one configuration."""
    from vam import config as config_module
    from vam import gateway as gw
    from vam.config import Settings
    from vam.evaluation import simulator
    from vam.indexing.service import IndexingService
    temporary, runtime, previous_settings = None, None, config_module._settings
    if models == 'simulator':
        from vam.gateway.limiter import LocalLimiter
        temporary = tempfile.TemporaryDirectory(prefix='evireel-eval-', ignore_cleanup_errors=True)
        data_root = Path(temporary.name) / 'data'
        settings = Settings(_env_file=None, data_root=str(data_root), enable_retrieval=True, rerank_enabled=False,
                            asr_enabled=False, query_include_latest_segment_video=False, study_auto_generate=False,
                            frame_store_path=str(Path(temporary.name) / 'default.sqlite3'), siliconflow_api_key='',
                            openrouter_api_key='', dashscope_api_key='', model_prices='',
                            # The simulator has no provider quota to protect; rate windows would only make results
                            # depend on timing. Real runs keep the configured limits (and report any throttling).
                            model_rpm_global=-1, model_rpm_provider=-1, model_tpm_provider=-1, model_rpm_workspace=-1,
                            model_inflight_provider=-1, model_inflight_workspace=-1, model_inflight_offline=-1,
                            **overrides)
        backend = simulator.LexicalBackend()
        ledger = EvalLedger(settings, max_calls=max_calls, max_tokens=max_tokens)
        gateway = gw.ModelGateway(settings, LocalLimiter(settings), ledger)
        service = IndexingService(settings, backend=backend, transcriber=object())
    else:
        from vam.server.assembly import assemble
        settings = Settings(**({'data_root': str(data_root)} | overrides))
        if not settings.platform_enabled and not standalone:
            raise SystemExit('real runs use the platform protections (DATABASE_URL: shared read leases, workspace '
                             'budgets, persistent usage ledger); pass --standalone only when no API or worker uses '
                             'this DATA_ROOT and no other process calls the same provider account')
        config_module._settings = settings
        backend = None
        try:
            service = IndexingService(settings, transcriber=object())
            runtime = assemble(settings, service)
        except BaseException:
            config_module._settings = previous_settings
            raise
        gateway = runtime.gateway
        # Per-question attribution and the run caps on top of the platform ledger (never instead of it).
        ledger = EvalLedger(settings, inner=gateway.ledger, max_calls=max_calls, max_tokens=max_tokens)
        gateway.ledger = ledger
        if hasattr(service.coordination, 'start_thread'):
            service.coordination.start_thread()   # lease renewal without one long-lived event loop
    previous = gw._gateway
    gw.configure(gateway)
    try:
        fixture_ids = None
        if models == 'simulator':
            from vam.evaluation.fixture import build
            fixture_ids = build(fixture, service, backend)
        service.runtime = runtime
        yield settings, service, ledger, fixture_ids, backend
    finally:
        gw.configure(previous)
        if runtime is not None:
            asyncio.run(service.coordination.close())
            config_module._settings = previous_settings
        if temporary is not None:
            # Shared read-only index handles keep their SQLite files open (Windows refuses to delete them).
            import gc
            service.readers.items.clear()
            gc.collect()
            temporary.cleanup()


def model_identities(settings, service, ledger, models):
    """Configured identities (one Settings object for providers, gateway and metadata) and the models the
    gateway actually saw, each with its price basis; ``consistent`` is False when a call used a model that is
    not among the configured ones."""
    from vam.config import get_settings
    from vam.gateway.ledger import price_basis
    if models == 'simulator':
        configured = {'embedding': service.backend.fingerprint(), 'rerank': 'sim-rerank', 'vlm': 'sim-vlm',
                      'llm': 'sim-llm', 'planner': 'sim-planner',
                      'note': 'deterministic simulator: scores verify the tool, not answer quality'}
        names = {'sim-embedding', 'sim-rerank', 'sim-vlm', 'sim-llm', 'sim-planner'}
    else:
        from vam.vision.backends import get_backend
        backend = service.backend or get_backend()
        embedding = backend.fingerprint() if hasattr(backend, 'fingerprint') else {'backend': type(backend).__name__}
        configured = {'provider': settings.provider_name, 'chat_and_planner': settings.model_id, 'vlm': settings.model_id_vlm,
                      'embedding': embedding, 'rerank': settings.rerank_model if settings.rerank_enabled else None,
                      'settings_are_process_settings': get_settings() is settings}
        names = {settings.model_id, settings.model_id_vlm, embedding.get('model'), settings.rerank_model}
        configured['price_basis'] = {m: price_basis(settings, m)[0] for m in sorted(n for n in names if n)}
    observed = ledger.observed_models()
    return {'configured': configured, 'observed': observed,
            'consistent': all(o['model'] in names for o in observed)}


def run(*, dataset_path, out, configs, models, fixture=None, data_root=None, split='all', top_k=5, followup='history',
        repeat=1, allow_paid=False, max_calls=None, max_tokens=None, overrides=None, standalone=False):
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
    unknown = [n for n in names if n not in CONFIGS]
    if unknown:
        raise SystemExit(f'unknown configs: {unknown}')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).parents[2]
    metadata = {'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'git': git_info(root),
                'environment': {'python': platform.python_version(), 'os': platform.platform(), 'cpu_count': os.cpu_count()},
                'dataset': {'path': str(dataset_path), 'sha256': ds.digest(dataset_path), 'split': split, **ds.describe(questions)},
                'fixture': {'path': str(fixture), 'sha256': ds.digest(fixture)} if fixture else None,
                'models': models, 'configs': {n: CONFIGS[n] for n in names},
                'top_k': top_k, 'followup': followup, 'repeat': repeat, 'metric_tolerance_s': metrics.TOLERANCE_S,
                'caps': {'max_calls': max_calls, 'max_tokens': max_tokens},
                'warm_up': 'one untimed query per config and video before measuring (not in results or usage)'}
    summaries = {}
    with _environment(models, data_root, fixture=fixture, max_calls=max_calls, max_tokens=max_tokens,
                      overrides=overrides or {}, standalone=standalone) as (settings, service, ledger, fixture_ids, backend):
        metadata['platform'] = service.runtime.describe(service) if service.runtime is not None else {
            'isolated': 'simulator: temporary DATA_ROOT, in-process limiter, in-memory ledger (no middleware)'}
        video_ids = {k: v['video_id'] for k, v in fixture_ids.items()} if fixture_ids else {q.video: q.video for q in questions}
        metadata['videos'] = fixture_ids or video_ids
        metadata['settings'] = {k: getattr(settings, k) for k in (
            'retrieval_candidates_per_source', 'retrieval_min_similarity', 'retrieval_max_evidence', 'retrieval_rrf_k',
            'retrieval_inspect_frames', 'route_min_similarity', 'route_min_keyword_coverage', 'route_escalation_timeout_s',
            'retrieval_max_turns', 'conversation_request_timeout_s', 'model_id', 'rerank_model')}
        sims = {'reranker': simulator.SimReranker(), 'answer': simulator.SimAnswerClient(),
                'inspector': simulator.SimInspector(), 'planner': simulator.SimPlanner} if models == 'simulator' else {}
        for name in names:
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
        metadata['model_identities'] = model_identities(settings, service, ledger, models)
    # A config with question errors (budget exhausted, agent timeout/failure, ...) is not a finished comparison.
    metadata['incomplete'] = {n: {'errors': s_['overall']['errors'], 'questions': s_['overall']['questions']}
                              for n, s_ in summaries.items() if s_['overall']['errors']}
    metadata['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    (out / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    (out / 'summary.json').write_text(json.dumps(summaries, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    (out / 'comparison.md').write_text(comparison(summaries, metadata), encoding='utf-8')
    for part in ('tune', 'holdout'):
        if any(part in s_['by_split'] for s_ in summaries.values()):
            text = f'Split: **{part}**\n\n' + comparison(summaries, metadata, split=part)
            (out / f'comparison-{part}.md').write_text(text, encoding='utf-8')
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
                     f"{calls:.1f} | {tokens:.0f} |"
                     + (f" channel failures {o['channel_failures']}, errors {o['errors']}" if o.get('channel_failures') or o.get('errors') else ''))
    note = (f"\nmodels: **{metadata['models']}** · dataset {metadata['dataset']['questions']} questions "
            f"({metadata['dataset']['by_label_source']}) · commit {metadata['git']['commit']} · top_k {metadata['top_k']}\n")
    if metadata['models'] == 'simulator':
        note += '\nSimulator run: the numbers verify the tooling and the routing logic; they are not answer quality.\n'
    for name, info in (metadata.get('incomplete') or {}).items():
        note += f"\n**{name} is incomplete**: {info['errors']} of {info['questions']} questions ended in an error (see results).\n"
    return head + '\n'.join(lines) + '\n' + note
