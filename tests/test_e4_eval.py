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
    assert not visual.sufficient and 'visual_question_without_strong_visual_evidence' in visual.reasons
    assert assess('幻灯片上写的是什么', [evidence('e1', 'transcript')], strong_text, s, inspected=True).sufficient


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
