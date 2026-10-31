"""E4: keyword channel, routing rules, metric definitions, dataset validation, and one end-to-end evaluation run
on the synthetic fixture with the deterministic simulator (no middleware, no provider calls)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vam.config import Settings

ROOT = Path(__file__).parents[1]
DATASET, FIXTURE = ROOT / 'eval/synthetic/questions.jsonl', ROOT / 'eval/synthetic/fixture.json'


def settings(**overrides):
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------- keyword channel
def test_tokens_keep_code_symbols_whole_and_split_and_chinese_as_bigrams():
    from vam.retrieval.keywords import tokenize
    tokens = tokenize('用 uf.find(u) 和 std::vector 实现并查集')
    assert {'uf.find', 'uf', 'find', 'std::vector', 'std', 'vector', 'u'} <= set(tokens)
    assert {'并查', '查集', '实现'} <= set(tokens)
    assert '__call__' in tokenize('实现 __call__ 方法')


def test_bm25_ranks_the_exact_symbol_first_and_scores_zero_without_overlap():
    from vam.retrieval.keywords import bm25
    docs = ['代码里我们写成 uf.find(u) 和 uf.union(u, v)', '今天讲最小生成树', 'heapq.heappush 放进堆里']
    scores = bm25('uf.find 是做什么的', docs)
    assert scores[0] > 0 and scores[1] == 0 and scores.index(max(scores)) == 0
    assert bm25('', docs) == [0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------- routing rules
def evidence(eid, source):
    return {'evidence_id': eid, 'source': source, 'start_t': 0, 'end_t': 1, 'text': ''}


def ranking(eid, source, similarity=None, coverage=None):
    details = {'similarity': similarity if similarity is not None else 1.0}
    if coverage is not None:
        details |= {'selection': 'keyword', 'coverage': coverage}
    return {'evidence_id': eid, 'source': source, 'score_details': details}


def test_route_assessment_rules():
    from vam.retrieval.routing import assess, needs_visual
    s = settings(route_min_similarity=0.5, route_min_keyword_coverage=0.6)
    strong_text = {'a': [ranking('e1', 'transcript', 0.8)]}
    assert assess('Kruskal 的复杂度？', [evidence('e1', 'transcript')], strong_text, s).sufficient
    weak = {'a': [ranking('e1', 'transcript', 0.2)]}
    result = assess('Kruskal 的复杂度？', [evidence('e1', 'transcript')], weak, s)
    assert not result.sufficient and result.reasons == ['no_strong_evidence']
    keyword = {'k': [ranking('e1', 'transcript', coverage=0.75)]}
    assert assess('uf.find 做什么', [evidence('e1', 'transcript')], keyword, s).sufficient
    # A question about the screen needs strong visual evidence, or an inspection.
    assert needs_visual('幻灯片上写的是什么') and not needs_visual('老师怎么解释的')
    visual = assess('幻灯片上写的是什么', [evidence('e1', 'transcript')], strong_text, s)
    assert not visual.sufficient and 'visual_question_needs_inspection' in visual.reasons
    # Strong event/frame evidence locates the moment but does not read the screen: still needs inspection.
    strong_frame = {'a': [ranking('e2', 'frame', 0.9)]}
    assert not assess('幻灯片上写的是什么', [evidence('e2', 'frame')], strong_frame, s).sufficient
    assert assess('幻灯片上写的是什么', [evidence('e1', 'transcript')], strong_text, s, inspected_ids={'e1'}).sufficient


# ---------------------------------------------------------------------------- metrics
def test_metric_definitions_and_denominators():
    from vam.evaluation.metrics import score_question, summarize
    q = {'answerable': True, 'answer_keywords': ['并查集'],
         'references': [{'start_t': 90, 'end_t': 105, 'source': 'transcript'}, {'start_t': 135, 'end_t': 135, 'source': 'frame'}]}
    ev = [{'source': 'transcript', 'start_t': 200, 'end_t': 210}, {'source': 'transcript', 'start_t': 100, 'end_t': 106},
          {'source': 'transcript', 'start_t': 134, 'end_t': 136}]
    answer = {'status': 'answered', 'answer': '用并查集', 'citations': [{'source': 'transcript', 'start_t': 100, 'end_t': 106}]}
    s = score_question(q, ev, answer)
    assert s['recall@1'] == 0 and s['recall@3'] == 0.5 and s['reciprocal_rank'] == 0.5   # frame ref needs a frame item
    assert s['citations_supported'] == 1 and s['time_error_s'] == 10 and s['keyword_proxy'] is True
    nq = {'answerable': False, 'references': [], 'answer_keywords': []}
    rows = [{'answerable': True, 'scores': s, 'latency': {'total_s': 1.0, 'retrieval_s': .2},
             'usage': {'calls': {'chat': 1}, 'input_tokens': 100, 'output_tokens': 10, 'cost': None}},
            {'answerable': False, 'scores': score_question(nq, [], {'status': 'refused', 'citations': []}),
             'latency': {'total_s': .1, 'retrieval_s': .1}, 'usage': {'calls': {}, 'input_tokens': 0, 'output_tokens': 0, 'cost': None}}]
    summary = summarize(rows)
    assert summary['abstention_on_unanswerable'] == 1.0 and summary['false_abstention_on_answerable'] == 0.0
    assert summary['citation_support_auto'] == 1.0 and summary['citation_support_human'] is None
    assert summary['cost_per_question'] is None and 'MODEL_PRICES' in summary['cost_reason']


def test_dataset_rejects_inconsistent_labels(tmp_path):
    from vam.evaluation.dataset import load
    bad = {'id': 'x', 'split': 'tune', 'category': 'no_answer', 'video': 'v', 'question': 'q', 'answerable': True,
           'references': [{'start_t': 1, 'end_t': 2}], 'label': {'source': 'synthetic'}}
    (tmp_path / 'q.jsonl').write_text(json.dumps(bad), encoding='utf-8')
    with pytest.raises(ValueError):
        load(tmp_path / 'q.jsonl')


def test_the_synthetic_set_is_consistent_with_its_fixture():
    from vam.evaluation.dataset import describe, load
    from vam.evaluation.fixture import check_references
    questions = load(DATASET)
    assert check_references(FIXTURE, questions) == []
    d = describe(questions)
    assert d['by_label_source'] == {'synthetic': len(questions)} and set(d['by_split']) == {'tune', 'holdout'}
    assert set(d['by_category']) == {'transcript', 'visual', 'cross_segment', 'no_answer', 'follow_up'}


def test_paid_runs_need_explicit_caps(tmp_path):
    from vam.evaluation.runner import run
    with pytest.raises(SystemExit, match='allow-paid'):
        run(dataset_path=DATASET, out=tmp_path, configs=['fusion'], models='real', data_root=tmp_path)


# ---------------------------------------------------------------------------- end to end (simulator)
def test_simulator_run_end_to_end_records_results_metadata_and_routes(tmp_path):
    from vam.evaluation.runner import run
    summaries, metadata = run(dataset_path=DATASET, out=tmp_path, configs=['fusion_keyword', 'auto'], models='simulator',
                              fixture=FIXTURE, split='holdout')
    assert metadata['models'] == 'simulator' and metadata['dataset']['by_label_source'] == {'synthetic': 15}
    for name in ('fusion_keyword', 'auto'):
        rows = [json.loads(l) for l in (tmp_path / f'{name}.results.jsonl').read_text(encoding='utf-8').splitlines()]
        assert len(rows) == 15 and all(r['error'] is None for r in rows)
        assert all(r['usage']['calls'] for r in rows)   # every question went through the gateway
        assert all(r['snapshot_versions']['visual'] for r in rows)
    auto = summaries['auto']['overall']
    assert set(auto['routes']) <= {'cheap', 'deep', 'refused'} and auto['questions'] == 15
    assert (tmp_path / 'comparison.md').read_text(encoding='utf-8').count('Simulator run') == 1


# ---------------------------------------------------------------------------- retrieval invariants on the fixture
@pytest.fixture
def corpus():
    """The synthetic corpus in a temporary DATA_ROOT, served by a real IndexingService with the simulator."""
    from vam.evaluation.runner import _environment
    with _environment('simulator', None, fixture=FIXTURE, max_calls=None, max_tokens=None, overrides={}) as env:
        yield env


def search_once(service, video_id, query, *, sources=None, keyword=False, min_time=0, max_time=float('inf'),
                mode='auto', answer_client=None):
    import asyncio
    from vam.evaluation import simulator

    async def go():
        async with service.retrieval_context(video_id) as ctx:
            ctx.use_keyword = keyword
            ctx.answer_client = answer_client or simulator.SimAnswerClient()
            ctx.inspector = simulator.SimInspector(latency_s=0)
            await ctx.search(query, top_k=5, sources=sources, min_time=min_time, max_time=max_time)
            evidence = await ctx.prepare(query, top_k=5)
            return ctx.snapshot, evidence, await ctx.answer(query, mode=mode)
    return asyncio.run(go())


def test_keyword_channel_recalls_chinese_terms_and_code_symbols(corpus):
    _, service, _, ids, _ = corpus
    from vam.memory.transcript import TranscriptStore
    row = service.registry.get(ids['algo']['video_id'])
    store = TranscriptStore(service.registry.asset(row['video_id'], f"memory/{row['transcript_generation']}.sqlite3"), row['video_id'])
    term = store.keyword_search('并查集', top_k=3)['results']
    assert (term[0]['start_t'], term[0]['coverage']) == (90.0, 1.0)              # "我们用并查集"
    symbol = store.keyword_search('heapq.heappop', top_k=3)['results']
    assert symbol[0]['start_t'] == 225.0                                          # the chunk naming heapq.heappop
    assert store.keyword_search('Bellman-Ford 负权环', top_k=3)['results'] == []   # nothing shares a token


def test_evidence_is_always_of_the_requested_video_and_its_pinned_versions(corpus):
    _, service, _, ids, _ = corpus
    vid = ids['algo']['video_id']
    snapshot, evidence, answer = search_once(service, vid, 'Kruskal 用什么判断连通', keyword=True)
    assert evidence and all(e['video_id'] == vid for e in evidence)
    for e in evidence:
        expected = snapshot.transcript_search_version if e['source'] == 'transcript' else snapshot.visual_generation
        assert e['index_version'] == expected
    assert all(c['video_id'] == vid for c in answer['citations'])


def test_candidates_filtered_to_nothing_are_refused_without_a_model_call(corpus):
    _, service, ledger, ids, _ = corpus
    ledger.current = 'empty-window'
    _, evidence, answer = search_once(service, ids['algo']['video_id'], '并查集', min_time=470, max_time=478,
                                      sources=['transcript'], keyword=True)
    assert evidence == [] and answer['status'] == 'refused' and answer['route']['path'] == 'refused'
    assert 'chat' not in ledger.usage('empty-window')['calls']                    # no answer model was called


def test_a_new_transcript_version_never_lets_old_evidence_be_cited(corpus):
    """After a new transcript version is published, a retrieval pins the new one; an answer citing evidence
    of the old version (an id from an earlier retrieval) is rejected by citation validation."""
    _, service, _, ids, backend = corpus
    from vam.asr.chunker import Chunk, Segment
    from vam.memory.transcript import TranscriptStore, embedding_fingerprint
    vid = ids['algo']['video_id']
    old_snapshot, old_evidence, _ = search_once(service, vid, '并查集', sources=['transcript'], keyword=True)
    row = service.registry.get(vid)
    store = TranscriptStore(service.registry.asset(vid, f"memory/{row['transcript_generation']}.sqlite3"), vid)
    version = 'f' * 32
    segment = Segment('s0', 90.0, 105.0, '判断连通我们改用并查集数据结构', 'zh', {}, '判断连通我们改用并查集数据结构', 90.0, 105.0, False)
    store.stage(version=version, transcription_key='v2', source_sha256='1' * 64, asr_fingerprint='v2', chunk_config='v2',
                embedding_identity=embedding_fingerprint(backend), segments=[segment],
                chunks=[Chunk('c0', 90.0, 105.0, segment.text, ['s0'])], language='zh', metadata={})
    store.publish(version, vectors=[backend._vector(segment.text)], dim=backend.dim)
    stale_id = old_evidence[0]['evidence_id']

    class CitesOldEvidence:
        async def chat(self, messages, temperature=0, max_tokens=1600):
            return json.dumps({'claims': [{'text': '用并查集', 'evidence_ids': [stale_id]}], 'warnings': []}), None
    snapshot, evidence, answer = search_once(service, vid, '并查集', sources=['transcript'], keyword=True, mode='cheap',
                                             answer_client=CitesOldEvidence())
    assert snapshot.transcript_search_version == version != old_snapshot.transcript_search_version
    assert all(e['index_version'] == version for e in evidence if e['source'] == 'transcript')
    assert stale_id not in {e['evidence_id'] for e in evidence}
    assert answer['status'] == 'uncertain' and answer['citations'] == []
    assert any(w['code'] == 'answer_validation_failed' for w in answer['warnings'])


def test_auto_escalates_visual_questions_and_uncertain_cheap_answers_once(corpus):
    _, service, _, ids, _ = corpus
    _, _, visual = search_once(service, ids['algo']['video_id'], '画面上写的作业提交平台是哪个？')
    assert visual['route']['path'] == 'deep' and visual['route']['escalated'] and visual['route']['rounds'] == 1
    assert visual['execution']['visual_checks']            # the frames were inspected before answering
    _, _, cheap = search_once(service, ids['algo']['video_id'], '画面上写的作业提交平台是哪个？', mode='cheap')
    assert cheap['status'] == 'refused' and not cheap['execution']['visual_checks']   # cheap never calls the VLM


def test_search_api_accepts_answer_mode_and_keyword(corpus):
    from fastapi.testclient import TestClient
    from vam.server.app import create_app
    settings, service, _, ids, backend = corpus
    app = create_app(settings=settings, backend=backend)
    with TestClient(app) as client:
        vid = ids['algo']['video_id']
        body = {'query': '红黑树的旋转操作怎么做', 'answer': True, 'answer_mode': 'auto', 'keyword': True, 'top_k': 5}
        response = client.post(f'/api/videos/{vid}/search', json=body)
        assert response.status_code == 200, response.text
        data = response.json()
        # Weak evidence, not a visual question: refused by rule, no model call needed.
        assert data['status'] == 'refused' and data['route']['path'] == 'refused'
        assert any(s.get('keyword') for s in data['execution']['searches'] if 'keyword' in s)
        bad = client.post(f'/api/videos/{vid}/search', json=body | {'answer_mode': 'turbo'})
        assert bad.status_code == 422



# ---------------------------------------------------------------------------- visual questions: final status
class CitesFirst:
    """Answer double: always cites a valid evidence id and never reports insufficiency."""
    def __init__(self, pick=lambda payload: payload['evidence'][0]['evidence_id']):
        self.pick = pick

    async def chat(self, messages, temperature=0, max_tokens=1600):
        payload = json.loads(messages[-1]['content'])
        return json.dumps({'claims': [{'text': '答案', 'evidence_ids': [self.pick(payload)]}], 'warnings': []}), None


def ask_visual(service, video_id, question, *, mode, inspector, answer_client=None, drop_visual=False):
    import asyncio

    async def go():
        async with service.retrieval_context(video_id) as ctx:
            ctx.answer_client = answer_client or CitesFirst()
            ctx.inspector = inspector
            await ctx.search(question, top_k=5)
            await ctx.prepare(question, top_k=5)
            if drop_visual:
                ctx.visual = None      # no usable frame store for the check
            return await ctx.answer(question, mode=mode)
    return asyncio.run(go())


VISUAL_Q = '画面上写的作业提交平台是哪个？'


@pytest.mark.parametrize('mode', ['auto', 'deep'])
@pytest.mark.parametrize('case', ['failed', 'timeout', 'empty', 'no_frames'])
def test_a_visual_question_is_not_answered_when_the_frames_were_not_actually_read(corpus, mode, case):
    import asyncio
    _, service, _, ids, _ = corpus
    if case == 'timeout' and mode == 'deep':
        pytest.skip('deep has no escalation deadline of its own (the request deadline applies)')
    service.settings.route_escalation_timeout_s = 0.3

    async def failing(frames, prompt):
        raise RuntimeError('vlm down')

    async def slow(frames, prompt):
        await asyncio.sleep(5)
        return 'late'

    async def empty(frames, prompt):
        return '   '
    inspector = {'failed': failing, 'timeout': slow, 'empty': empty, 'no_frames': empty}[case]
    answer = ask_visual(service, ids['algo']['video_id'], VISUAL_Q, mode=mode, inspector=inspector,
                        drop_visual=case == 'no_frames')
    expected = {'failed': 'visual_inspection_failed', 'timeout': 'visual_inspection_timeout',
                'empty': 'visual_inspection_empty', 'no_frames': 'visual_inspection_unavailable'}[case]
    # The answer double returned a valid citation and no insufficiency flag: still not "answered".
    assert answer['citations'] and answer['status'] == 'uncertain'
    assert expected in answer['route']['status_reasons']
    assert any(w['code'] == 'visual_evidence_unverified' for w in answer['warnings'])
    if mode == 'auto':
        assert answer['route']['after_escalation']['sufficient'] is False


def test_a_visual_answer_must_cite_evidence_whose_frames_were_read(corpus):
    from vam.evaluation import simulator
    _, service, _, ids, _ = corpus
    vid = ids['algo']['video_id']
    def inspected(payload):
        return payload['visual_checks'][0]['evidence_ids'][0]
    read = ask_visual(service, vid, VISUAL_Q, mode='auto', inspector=simulator.SimInspector(latency_s=0),
                      answer_client=CitesFirst(inspected))
    assert read['status'] == 'answered' and read['route']['status_reasons'] == []
    assert read['route']['after_escalation']['sufficient'] is True

    def uninspected(payload):
        seen = {eid for check in payload['visual_checks'] for eid in check['evidence_ids']}
        return next(e['evidence_id'] for e in payload['evidence'] if e['evidence_id'] not in seen)
    service.settings.retrieval_inspect_frames = 1      # one frame read: some evidence stays unread
    other = ask_visual(service, vid, VISUAL_Q, mode='auto', inspector=simulator.SimInspector(latency_s=0),
                       answer_client=CitesFirst(uninspected))
    assert other['status'] == 'uncertain' and 'citations_not_visually_verified' in other['route']['status_reasons']


def test_route_records_model_uncertainty_separately_from_the_escalation_decision(corpus):
    _, service, _, ids, _ = corpus

    class Unsure:
        async def chat(self, messages, temperature=0, max_tokens=1600):
            payload = json.loads(messages[-1]['content'])
            return json.dumps({'claims': [{'text': '?', 'evidence_ids': [payload['evidence'][0]['evidence_id']]}],
                               'warnings': ['insufficient_evidence']}), None
    _, _, cheap = search_once(service, ids['algo']['video_id'], '并查集', sources=['transcript'], keyword=True,
                              mode='cheap', answer_client=Unsure())
    route = cheap['route']
    assert route['path'] == 'cheap' and route['cheap_answer_uncertain'] is True and route['escalated'] is False
    assert route['escalation_reason'] == 'not_allowed_in_cheap_mode' and cheap['status'] == 'uncertain'
    _, _, auto = search_once(service, ids['algo']['video_id'], '并查集', sources=['transcript'], keyword=True,
                             mode='auto', answer_client=Unsure())
    assert auto['route']['cheap_answer_uncertain'] is True
    assert auto['route']['escalated'] == (auto['route']['escalation_reason'] == 'cheap_answer_uncertain')


# ---------------------------------------------------------------------------- evaluation ledger
class Call:
    def __init__(self, n, est_in=10, est_out=0, model='m'):
        self.call_id, self.operation, self.model, self.provider, self.workspace = f'c{n}', 'chat', model, 'p', 'ws'
        self.attempt, self.est_input_tokens, self.est_output_tokens = 1, est_in, est_out


def test_eval_ledger_counts_a_reported_zero_as_zero_not_as_the_estimate():
    from vam.evaluation.runner import EvalLedger
    ledger = EvalLedger(settings(), max_tokens=25)
    ledger.current = ('cfg', 0, 'q1')
    for n in range(3):          # each estimated at 10 tokens, each reported by the provider as 0
        ledger.reserve(Call(n))
        ledger.settle(Call(n), 0, 0, 'provider')
    assert ledger._used() == 0 and ledger.usage(('cfg', 0, 'q1'))['input_tokens'] == 0
    ledger.reserve(Call(9, est_in=20))          # 0 + 20 <= 25: admitted (the estimate fallback would refuse)


class PlatformLedger:
    persistent = True

    def __init__(self):
        self.events, self.refuse = [], False

    def reserve(self, call):
        from vam.video_registry import DomainError
        if self.refuse:
            raise DomainError(429, 'model_budget_exceeded', 'workspace budget')
        self.events.append(('reserve', call.call_id))

    def settle(self, call, i, o, source):
        self.events.append(('settle', call.call_id, i, o))

    def fail(self, call, code):
        self.events.append(('fail', call.call_id, code))

    def unknown(self, call, code):
        self.events.append(('unknown', call.call_id, code))

    def reconcile(self):
        return 7


def test_eval_ledger_layers_caps_and_attribution_on_the_platform_ledger():
    from vam.evaluation.runner import EvalLedger
    from vam.video_registry import DomainError
    inner = PlatformLedger()
    ledger = EvalLedger(settings(), inner=inner, max_calls=2)
    assert ledger.persistent and ledger.reconcile() == 7
    ledger.current = ('cfg', 0, 'q1')
    ledger.reserve(Call(1))
    ledger.settle(Call(1), 3, 4, 'provider')
    inner.refuse = True
    with pytest.raises(DomainError, match='workspace budget'):     # the workspace budget still applies
        ledger.reserve(Call(2))
    assert len(ledger.rows) == 1                                   # refused upstream: no evaluation row
    inner.refuse = False
    ledger.reserve(Call(3))
    ledger.unknown(Call(3), 'timeout')
    with pytest.raises(DomainError) as cap:                        # run cap: refused before the platform
        ledger.reserve(Call(4))
    assert cap.value.code == 'eval_budget_exceeded' and ('reserve', 'c4') not in inner.events
    assert inner.events == [('reserve', 'c1'), ('settle', 'c1', 3, 4), ('reserve', 'c3'), ('unknown', 'c3', 'timeout')]
    assert ledger.usage(('cfg', 0, 'q1'))['unknown_outcomes'] == 1


# ---------------------------------------------------------------------------- real-mode wiring (no provider call)
def test_real_runs_refuse_to_bypass_the_platform_and_use_one_settings_object(tmp_path):
    from vam import config as config_module
    from vam.config import get_settings
    from vam.evaluation.runner import _environment, model_identities
    base = dict(_env_file=None, enable_retrieval=False, database_url='', redis_url='', siliconflow_api_key='',
                embedding_model_override='probe-embedding-model')
    before = config_module._settings
    with pytest.raises(SystemExit, match='platform protections'):
        with _environment('real', tmp_path / 'a', fixture=None, max_calls=1, max_tokens=1, overrides=base):
            pass
    assert config_module._settings is before
    with _environment('real', tmp_path / 'b', fixture=None, max_calls=1, max_tokens=1, overrides=base,
                      standalone=True) as (s, service, ledger, _, _):
        assert get_settings() is s and s.embedding_model_id == 'probe-embedding-model'
        assert service.runtime.describe(service)['evaluation_ledger_on_top'] is True
        identities = model_identities(s, service, ledger, 'real')
        assert identities['configured']['settings_are_process_settings'] is True
        assert identities['configured']['chat_and_planner'] == s.model_id and identities['consistent']
    assert config_module._settings is before


# ---------------------------------------------------------------------------- multi-turn agent
def agent_rows(corpus, planner_factory, *, questions=('syn-02', 'syn-04'), max_calls=None, **overrides):
    import asyncio
    from vam.evaluation import dataset as ds, simulator
    from vam.evaluation.runner import CONFIGS, run_config
    _, service, ledger, ids, _ = corpus
    for key, value in overrides.items():
        setattr(service.settings, key, value)
    ledger.max_calls = max_calls
    picked = [q for q in ds.load(DATASET) if q.id in questions]
    sims = {'reranker': simulator.SimReranker(latency_s=0), 'answer': simulator.SimAnswerClient(latency_s=0),
            'inspector': simulator.SimInspector(latency_s=0), 'planner': planner_factory}
    video_ids = {k: v['video_id'] for k, v in ids.items()}
    return asyncio.run(run_config(service, picked, video_ids, 'agent', CONFIGS['agent'], models='simulator', top_k=5,
                                  followup='history', ledger=ledger, sims=sims))


def test_agent_config_runs_the_tool_loop_with_the_same_versions_and_usage_records(corpus):
    from vam.evaluation import simulator
    rows = agent_rows(corpus, lambda goal: simulator.SimPlanner(goal, latency_s=0))
    assert len(rows) == 2
    for row in rows:
        assert row['error'] is None and row['agent']['planner_turns'] == 2 and row['agent']['searches'] >= 1
        assert row['agent']['inspections'] >= 1 and row['snapshot_versions']['visual']
        assert row['usage']['calls']['chat'] >= 3 and row['answer']['status'] in {'answered', 'uncertain'}
        assert row['route']['mode'] == 'deep'


def test_agent_turns_and_candidates_are_bounded(corpus):
    from vam.evaluation import simulator

    class Greedy(simulator.SimPlanner):
        async def chat(self, messages, temperature=0, max_tokens=500):
            self.turn = 0       # never answers: always another search, asking for 200 candidates
            text, usage = await super().chat(messages, temperature, max_tokens)
            plan = json.loads(text)
            plan['queries'][0]['top_k'] = 200
            return json.dumps(plan), usage
    row = agent_rows(corpus, lambda goal: Greedy(goal, latency_s=0), questions=('syn-02',), retrieval_max_turns=3)[0]
    assert row['error'] is None and row['agent']['planner_turns'] == 3            # RETRIEVAL_MAX_TURNS
    assert 0 < row['agent']['max_candidates'] <= corpus[1].settings.retrieval_max_evidence


def test_agent_timeout_budget_exhaustion_and_planner_failure_are_errors_not_answers(corpus):
    import asyncio
    from vam.evaluation import simulator

    class Slow(simulator.SimPlanner):
        async def chat(self, messages, temperature=0, max_tokens=500):
            await asyncio.sleep(5)

    class Broken(simulator.SimPlanner):
        async def chat(self, messages, temperature=0, max_tokens=500):
            raise RuntimeError('planner crashed')
    timeout = agent_rows(corpus, Slow, questions=('syn-02',), conversation_request_timeout_s=0.5)
    assert timeout[0]['error'] == 'agent_timeout' and timeout[0]['answer']['status'] == 'error'
    broken = agent_rows(corpus, Broken, questions=('syn-02',), conversation_request_timeout_s=60)
    assert broken[0]['error'] == 'agent_failed' and broken[0]['agent']['exception'] == 'RuntimeError'
    ledger = corpus[2]
    budget = agent_rows(corpus, lambda goal: simulator.SimPlanner(goal, latency_s=0), max_calls=len(ledger.rows) + 3)
    assert 'eval_budget_exceeded' in {r['error'] for r in budget}
    assert all(r['answer']['status'] == 'error' for r in budget if r['error'])


def test_a_run_with_failed_agent_questions_is_marked_incomplete(tmp_path):
    from vam.evaluation.runner import run
    summaries, metadata = run(dataset_path=DATASET, out=tmp_path, configs=['agent'], models='simulator',
                              fixture=FIXTURE, split='holdout', max_calls=40)
    assert 'not_run' not in metadata and metadata['incomplete']['agent']['errors'] > 0
    assert '**agent is incomplete**' in (tmp_path / 'comparison.md').read_text(encoding='utf-8')
    assert metadata['model_identities']['consistent']
    assert {o['model'] for o in metadata['model_identities']['observed']} >= {'sim-planner', 'sim-llm'}


# ---------------------------------------------------------------------------- run caps under concurrency (review 2)
class GatedLedger(PlatformLedger):
    """Platform ledger double whose reservation blocks until released (the PostgreSQL wait window), then
    admits, refuses or raises."""
    def __init__(self, outcome='admit'):
        super().__init__()
        import threading
        self.entered, self.release, self.outcome, self.calls = threading.Event(), threading.Event(), outcome, 0

    def reserve(self, call):
        from vam.video_registry import DomainError
        self.calls += 1
        self.entered.set()
        assert self.release.wait(10)
        if self.outcome == 'refuse':
            raise DomainError(429, 'model_budget_exceeded', 'workspace budget')
        if self.outcome == 'raise':
            raise RuntimeError('database unavailable')
        self.events.append(('reserve', call.call_id))


def cap_gateway(inner=None, **caps):
    from vam.evaluation.runner import EvalLedger
    from vam.gateway import ModelGateway
    from vam.gateway.limiter import LocalLimiter
    s = settings(model_rpm_global=-1, model_rpm_provider=-1, model_rpm_workspace=-1, model_tpm_provider=-1,
                 model_inflight_provider=-1, model_inflight_workspace=-1, model_retry_base_s=0)
    ledger = EvalLedger(s, inner=inner, **caps)
    ledger.current = ('cfg', 0, 'q1')
    return ModelGateway(s, LocalLimiter(s), ledger), ledger


def provider(sent, *, tokens=10, error=None):
    async def attempt():
        sent.append(1)
        if error is not None:
            raise error
        return 'ok', {'input_tokens': tokens, 'output_tokens': 0}
    return attempt


@pytest.mark.parametrize('caps', [{'max_calls': 1}, {'max_tokens': 10}], ids=['last_call', 'last_tokens'])
def test_parallel_calls_cannot_both_take_the_last_share_of_the_run_budget(caps):
    """The reviewer's probe: two calls race for the last call / the last 10 tokens while the platform
    reservation of the first one is still pending. Only one is sent; the other's attempt never runs."""
    import asyncio
    from vam.runtime import run_sync
    from vam.video_registry import DomainError
    inner = GatedLedger()
    gateway, ledger = cap_gateway(inner, **caps)
    sent = []

    async def race():
        first = asyncio.create_task(gateway.run(operation='chat', model='m', attempt_fn=provider(sent),
                                                est_input_tokens=10, attempts=1))
        await run_sync(inner.entered.wait, 5)                  # the first call holds the budget, platform pending
        second = await asyncio.gather(gateway.run(operation='chat', model='m', attempt_fn=provider(sent),
                                                  est_input_tokens=10, attempts=1), return_exceptions=True)
        inner.release.set()
        return await first, second[0]
    ok, refused = asyncio.run(race())
    assert ok == 'ok' and isinstance(refused, DomainError) and refused.code == 'eval_budget_exceeded'
    assert len(sent) == 1 and inner.calls == 1 and len(ledger.rows) == 1 and ledger._used() == 10


def test_many_threads_never_exceed_the_run_caps():
    import threading
    from vam.video_registry import DomainError
    gateway, ledger = cap_gateway(PlatformLedger(), max_calls=5, max_tokens=1000)
    sent, refused, barrier = [], [], threading.Barrier(20)

    def one():
        barrier.wait()
        try:
            gateway.run_sync(operation='embedding', model='m', attempts=1, est_input_tokens=10,
                             attempt_fn=lambda timeout: (sent.append(1), ('ok', {'input_tokens': 10, 'output_tokens': 0}))[1])
        except DomainError as exc:
            refused.append(exc.code)
    threads = [threading.Thread(target=one) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(sent) == 5 and refused == ['eval_budget_exceeded'] * 15 and len(ledger.rows) == 5


@pytest.mark.parametrize('outcome', ['refuse', 'raise'])
def test_a_platform_refusal_or_error_after_blocking_gives_the_run_budget_back(outcome):
    """While the platform reservation blocks, the run budget is held (a competing call is refused); when the
    platform then refuses or fails, the hold is released, the attempt never runs, and the next call fits."""
    import asyncio
    from vam.runtime import run_sync
    from vam.video_registry import DomainError
    inner = GatedLedger(outcome)
    gateway, ledger = cap_gateway(inner, max_calls=1, max_tokens=10)
    sent = []

    async def go():
        first = asyncio.create_task(gateway.run(operation='chat', model='m', attempt_fn=provider(sent),
                                                est_input_tokens=10, attempts=1))
        await run_sync(inner.entered.wait, 5)
        with pytest.raises(DomainError):                       # budget held during the platform wait
            await gateway.run(operation='chat', model='m', attempt_fn=provider(sent), est_input_tokens=10, attempts=1)
        inner.release.set()
        return (await asyncio.gather(first, return_exceptions=True))[0]
    error = asyncio.run(go())
    expected = DomainError if outcome == 'refuse' else RuntimeError
    assert isinstance(error, expected) and sent == [] and ledger.rows == [] and ledger._used() == 0
    inner.outcome = 'admit'
    assert asyncio.run(gateway.run(operation='chat', model='m', attempt_fn=provider(sent), est_input_tokens=10,
                                   attempts=1)) == 'ok' and sent == [1]


def test_a_call_cancelled_during_the_platform_reservation_is_never_sent_and_is_closed():
    import asyncio
    from vam.runtime import run_sync
    inner = GatedLedger()
    gateway, ledger = cap_gateway(inner, max_calls=2, max_tokens=20)
    sent = []

    async def go():
        task = asyncio.create_task(gateway.run(operation='chat', model='m', attempt_fn=provider(sent),
                                               est_input_tokens=10, attempts=1))
        await run_sync(inner.entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        inner.release.set()                                    # the reservation completes in its thread
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    assert sent == [] and [r['status'] for r in ledger.rows] == ['failed']
    assert ledger.rows[0]['error'] == 'cancelled_before_send' and ledger._used() == 0
    assert inner.events[-1] == ('fail', ledger.rows[0]['call_id'], 'cancelled_before_send')


def test_settled_failed_and_unknown_outcomes_count_against_the_run_budget_as_recorded():
    """settled: the billed tokens; failed (certainly not billed): no tokens, still an attempt; unknown (may
    have been billed): the estimate."""
    import asyncio
    from vam.gateway import ProviderError
    from vam.video_registry import DomainError
    gateway, ledger = cap_gateway(PlatformLedger(), max_calls=4, max_tokens=25)
    sent = []

    def call(attempt):
        return asyncio.run(gateway.run(operation='chat', model='m', attempt_fn=attempt, est_input_tokens=10, attempts=1))
    assert call(provider(sent, tokens=3)) == 'ok' and ledger._used() == 3            # settled: billed, not estimate
    with pytest.raises(ProviderError):
        call(provider(sent, error=ProviderError('provider_http_400', status=400, retryable=False, sent=False)))
    assert ledger._used() == 3 and len(ledger.rows) == 2                            # failed: 0 tokens, 1 call
    with pytest.raises(ProviderError):
        call(provider(sent, error=ProviderError('provider_timeout', retryable=True, sent=True)))
    assert ledger._used() == 13                                                     # unknown: the estimate
    assert [r['status'] for r in ledger.rows] == ['settled', 'failed', 'unknown']
    with pytest.raises(DomainError) as cap:                                         # 13 + 20 > 25: not sent
        asyncio.run(gateway.run(operation='chat', model='m', attempt_fn=provider(sent), est_input_tokens=20, attempts=1))
    assert cap.value.code == 'eval_budget_exceeded' and len(sent) == 3
    usage = ledger.usage(('cfg', 0, 'q1'))
    assert usage['attempts'] == 3 and usage['unknown_outcomes'] == 1 and usage['input_tokens'] == 13
