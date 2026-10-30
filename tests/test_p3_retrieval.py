"""P3 evidence contracts with real storage/media/HTTP and explicit model mocks."""
import asyncio
from contextlib import contextmanager
import json
import subprocess
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest
from PIL import Image, ImageDraw

import test_p2_transcripts as p2
from vam import prompts
from vam.llm.openrouter import OpenRouterClient
from vam.protocol import PlannerActionAdapter, PlannerTimeRange
from vam.retrieval.agent_search import hybrid_event_frame_search
from vam.retrieval.evidence import adapt_hit, validate_answer
from vam.retrieval.frame_store import FrameStore
from vam.retrieval.fusion import fuse
from vam.retrieval.pipeline import active_search, choose_sources, SearchContext
from vam.retrieval.rerank import CohereReranker, MockReranker, rerank_text, validate_ranks
from vam.server.app import create_app

settings = p2.settings
audio_media = p2.audio_media
media = p2.media
transcriber = p2.transcriber


class Models:
    def __init__(self):
        self.invalid_id = False
        self.invalid_text = None
        self.conflict = False
        self.inspections = []
        self.plans = []
        self.planner_turns = 0
        self.inspection_text = 'Visible red slide; compare the quoted speech separately.'

    async def chat(self, client, messages, **kwargs):
        system = messages[0]['content']
        content = messages[-1]['content']
        if system == prompts.grounded_answer_system():
            data = json.loads(content)
            ids = [e['evidence_id'] for e in data['evidence']]
            if self.invalid_id:
                ids = ['ev_' + 'f'*32]
            return json.dumps({'claims': [{'text': self.invalid_text or '教师口述与相关画面已作为独立证据列出。', 'evidence_ids': ids[:4]}],
                               'warnings': ['speech_visual_conflict'] if self.conflict else []}), {}
        if system == prompts.ws_agent_system():
            return json.dumps({'type': 'tool', 'name': 'retrieve', 'args': {'question': content}}), {}
        if system == prompts.agent_planner_system():
            if self.plans:
                return json.dumps(self.plans.pop(0)), {}
            self.planner_turns += 1
            if self.planner_turns % 2:
                return json.dumps({'action': 'search', 'queries': [{'q': 'blue', 'top_k': 5, 'inspect_k': 3, 'threshold': 0}],
                                   'sources': ['transcript', 'frame'], 'joint_inspection': True}), {}
            return json.dumps({'action': 'answer', 'response': 'An unvalidated draft that must not escape.'}), {}
        if isinstance(content, list) and system.startswith('You are the Multimodal Inspector'):
            self.inspections.append(content)
        return self.inspection_text, {}


@contextmanager
def client_for(settings, transcriber, *, backend=None, reranker=None, models=None):
    models = models or Models()
    async def chat(client, messages, **kwargs):
        return await models.chat(client, messages, **kwargs)
    with patch.object(OpenRouterClient, 'chat', new=chat):
        with TestClient(create_app(settings=settings, backend=backend or p2.TranscriptBackend(),
                                   transcriber=transcriber, reranker=reranker)) as client:
            yield client, models


def query(client, vid, **options):
    response = client.post(f'/api/videos/{vid}/search', json={'query': 'red', **options})
    assert response.status_code == 200, response.text
    return response.json()


def evidence(record='a', source='frame', score=1, version='v1', video='v_'+'a'*32):
    return adapt_hit({'frame_id': record if source == 'frame' else None,
                      'doc_id': record if source != 'frame' else None, 'start_t': 0, 'end_t': 0 if source == 'frame' else 1,
                      'text': '' if source == 'frame' else record, 'score': score},
                     video_id=video, source=source, version=version, duration=2)


def test_rrf_hand_calculation_scale_and_duplicate_ids():
    a, b = evidence('a', score=10000), evidence('b', score=.001)
    result = fuse({'channel1': [a, a, b], 'channel2': [b, a]}, k=60)
    assert len(result) == 2
    assert result[0]['score'] == pytest.approx(1/61 + 1/62)
    assert result[1]['score'] == pytest.approx(1/61 + 1/62)
    assert result == fuse({'channel2': [b, a], 'channel1': [a, a, b]}, k=60)
    assert fuse({}) == []
    assert a['score'] == 10000  # fusion does not mutate channel scores


def test_multiquery_fuses_full_candidate_pool_before_global_topk(settings):
    row={'video_id':'v_'+'a'*32,'duration':20,'active_generation':'1'*32}
    scope=SearchContext(row=row,visual=SimpleNamespace(get_frame_by_id=AsyncMock(return_value=object())),
                        transcript=None,backend=p2.TranscriptBackend(),settings=settings)
    async def recall(source,question,**kwargs):
        return [{'frame_id':'a' if question=='first' else 'b','t':0,'score':1},
                {'frame_id':'common','t':2,'score':.8}]
    scope.recall=recall
    async def run():
        await scope.search('first',top_k=1,sources=['frame'])
        await scope.search('second',top_k=1,sources=['frame'])
        return await scope.prepare('combined',top_k=1)
    result=asyncio.run(run())
    assert result[0]['frame_id']=='common'
    assert result[0]['score']==pytest.approx(2/62)


@pytest.mark.parametrize('k', [0, -1, float('inf')])
def test_rrf_invalid_k(k):
    with pytest.raises(ValueError):
        fuse({}, k=k)


def test_identity_includes_video_source_version_and_record():
    base = evidence()
    variants = [evidence(version='v2'), evidence(video='v_'+'b'*32), evidence('b'), evidence(source='event')]
    assert all(v['evidence_id'] != base['evidence_id'] for v in variants)
    assert evidence()['evidence_id'] == base['evidence_id']
    with pytest.raises(ValueError):
        adapt_hit({'frame_id': 'f', 't': 9}, video_id='v', source='frame', version='v1', duration=2)


def test_answer_ids_and_times_come_from_records():
    e = evidence(source='transcript')
    answer, citations, _ = validate_answer({'claims': [{'text': 'A supported concept.', 'evidence_ids': [e['evidence_id']]}]}, [e])
    assert e['evidence_id'] in answer
    assert citations[0]['start_t'] == 0 and citations[0]['end_t'] == 1
    with pytest.raises(ValueError):
        validate_answer({'claims': [{'text': 'Fake.', 'evidence_ids': ['ev_missing']}]}, [e])
    with pytest.raises(ValueError):
        validate_answer({'claims': [{'text': 'Mixed.', 'evidence_ids': [e['evidence_id']]}]}, [e, evidence(video='v_'+'b'*32)])


@pytest.mark.parametrize('text', ['At 99.0s.', '第99秒的位置', 'At 01:23.', 'See https://other.example/', 'ev_forged'])
def test_model_cannot_invent_resource_or_time_references(text):
    e = evidence()
    with pytest.raises(ValueError):
        validate_answer({'claims': [{'text': text, 'evidence_ids': [e['evidence_id']]}]}, [e])


def test_all_sources_nested_anchors_and_intent_selection():
    time_range = PlannerTimeRange.model_validate({'start_anchor': {'query': 'spoken', 'sources': ['transcript']},
                                                   'end_anchor': {'query': 'slide', 'candidate_source_groups': [['frame', 'transcript']]}})
    assert time_range.start_anchor.sources == ['transcript']
    assert choose_sources('老师说了什么')[0] == 'transcript'
    assert choose_sources('PPT公式')[:2] == ['frame', 'transcript']
    assert choose_sources('课程概览')[0] == 'summary'
    assert choose_sources('发生了什么')[0] == 'event'
    assert PlannerActionAdapter.parse_payload({'action':'search','queries':[{'q':'blue'}],'sources':['transcript']}).sources == ['transcript']


def test_legacy_planner_only_advertises_available_visual_sources():
    legacy=prompts.agent_planner_system(include_transcripts=False)
    assert 'transcript' not in legacy
    assert "['frame', 'event', 'summary']" in legacy
    assert 'transcript' in prompts.agent_planner_system()


def test_rerank_contract_and_blank_frame_slots():
    rows = [evidence('a', source='event'), evidence('blank'), evidence('b', source='transcript')]
    reranker = MockReranker([.1, .9])
    result, info = asyncio.run(rerank_text(rows, query='concept', reranker=reranker, limit=10, timeout=1, text_limit=100))
    assert [r['doc_id'] for r in result] == ['b', None, 'a']
    assert result[1]['frame_id'] == 'blank'
    assert info['covered'] == 2 and info['uncovered'] == 1 and info['duration_s'] >= 0
    assert len(reranker.calls) == 1 and reranker.calls[0][1] == ['a', 'b']


@pytest.mark.parametrize('results', [[], [{'index':0,'relevance_score':1},{'index':0,'relevance_score':0}],
                                      [{'index':2,'relevance_score':1},{'index':1,'relevance_score':0}],
                                      [{'index':0,'relevance_score':float('nan')},{'index':1,'relevance_score':0}]])
def test_malformed_rerank_rejected(results):
    with pytest.raises(ValueError):
        validate_ranks(results, 2)


def test_real_cohere_adapter_serialization_with_mock_http(settings):
    settings.rerank_api_key = 'explicit-test-key'
    def handle(request):
        assert str(request.url) == 'https://api.cohere.com/v2/rerank'
        assert request.headers['Authorization'] == 'Bearer explicit-test-key'
        data = json.loads(request.content)
        assert data['documents'] == ['one', 'two'] and data['top_n'] == 2 and data['model'] == settings.rerank_model
        return httpx.Response(200, json={'results':[{'index':1,'relevance_score':.8},{'index':0,'relevance_score':.1}]})
    adapter = CohereReranker(settings, transport=httpx.MockTransport(handle))
    assert asyncio.run(adapter.rank('query', ['one', 'two'])) == [(1,.8), (0,.1)]


def test_http_four_channels_and_no_automatic_summary(settings, audio_media, transcriber):
    with client_for(settings, transcriber) as (client, models):
        vid, _ = p2.ingest(client, audio_media)
        data = query(client, vid, sources=['summary'])
        assert any(w['code'] == 'summary_empty_fallback_existing' for w in data['warnings'])
        assert client.get(f'/api/videos/{vid}/memory-documents?kind=summary').json()['total'] == 0
        assert not models.inspections  # search-only HTTP does not run a VLM
        with client.websocket_connect('/ws/agent') as ws:
            ws.receive_json()
            ws.send_json({'type':'tool', 'name':'summarize','video_id':vid,
                          'args':{'min_time':0,'max_time':2,'time_mode':'relative','granularity_seconds':30,'prompt':'summary'}})
            while True:
                event = ws.receive_json()
                if event['type'] in {'final','error'}:
                    assert event['type'] == 'final', event
                    assert all(e['source'] == 'summary' for e in event['result']['evidence'])
                    break
        data = query(client, vid, sources=['frame','event','summary','transcript'], top_k=20)
        assert {e['source'] for e in data['evidence']} == {'frame','event','summary','transcript'}
        assert all(e['video_id'] == vid and e['end_t'] >= e['start_t'] for e in data['evidence'])
        assert 'data:image' not in json.dumps(data)


def test_spoken_intent_breaks_rrf_ties_toward_transcript(settings,audio_media,transcriber):
    with client_for(settings,transcriber) as (client,_):
        vid,_=p2.ingest(client,audio_media)
        data=query(client,vid,query='老师说 red concept',top_k=1)
        assert data['evidence'][0]['source']=='transcript'
        assert data['execution']['searches'][0]['sources'][0]=='transcript'


def test_multiquery_rrf_dedup_and_baseline(settings, audio_media, transcriber):
    with client_for(settings, transcriber) as (client, _):
        vid, _ = p2.ingest(client, audio_media)
        one = query(client, vid, sources=['frame'], queries=[])
        repeated = query(client, vid, sources=['frame'], queries=['RED', ' red '])
        assert [(e['evidence_id'], e['score']) for e in one['evidence']] == [(e['evidence_id'], e['score']) for e in repeated['evidence']]
        many = query(client, vid, sources=['frame'], queries=['red concept'])
        for e in many['evidence']:
            assert e['score'] == pytest.approx(sum(c['rrf'] for c in e['score_details']['channels'].values()))
        assert len({e['evidence_id'] for e in many['evidence']}) == len(many['evidence'])
        baseline = query(client, vid, sources=['transcript','summary'], fusion='legacy')
        assert set(e['source'] for e in baseline['evidence']) <= {'frame','event'}
        assert baseline['execution']['searches'][0]['sources'] == ['frame','event']


def test_cross_video_and_closed_time_filter(settings, audio_media, transcriber):
    with client_for(settings, transcriber) as (client, _):
        a, _ = p2.ingest(client, audio_media)
        b, _ = p2.ingest(client, audio_media, 'blue-audio.mp4')
        ea, eb = query(client, a)['evidence'], query(client, b, query='blue')['evidence']
        assert not {e['evidence_id'] for e in ea} & {e['evidence_id'] for e in eb}
        assert all(e['video_id'] == b for e in eb)
        narrow = query(client, a, query='blue', sources=['transcript'], min_time=1.05, max_time=1.8)
        assert narrow['evidence']
        assert all(e['end_t'] >= 1.05 and e['start_t'] <= 1.8 for e in narrow['evidence'])
        boundary = query(client, a, sources=['frame'], min_time=1, max_time=1)
        assert any(e['start_t'] == 1 for e in boundary['evidence'])


def test_evidence_versions_after_visual_rebuild(settings, audio_media, transcriber):
    with client_for(settings, transcriber) as (client, _):
        vid, _ = p2.ingest(client, audio_media)
        before = query(client, vid, sources=['frame'])['evidence']
        speech = query(client, vid, sources=['transcript'])['evidence']
        jid = client.post(f'/api/videos/{vid}/index').json()['job_id']
        assert p2.terminal(client, jid)['status'] == 'succeeded'
        after = query(client, vid, sources=['frame'])['evidence']
        assert not {e['evidence_id'] for e in before} & {e['evidence_id'] for e in after}
        assert client.get(before[0]['image_url']).status_code == 409
        speech_after = query(client, vid, sources=['transcript'])['evidence']
        assert {e['evidence_id'] for e in speech if e['source']=='transcript'} == {e['evidence_id'] for e in speech_after if e['source']=='transcript'}


def test_missing_model_channel_does_not_mix_vectors(settings, audio_media, transcriber):
    backend = p2.TranscriptBackend()
    with client_for(settings, transcriber, backend=backend) as (client, _):
        vid, _ = p2.ingest(client, audio_media)
        backend.name = 'different-model-same-dimension'
        data = query(client, vid)
        assert not data['evidence']
        assert {'incompatible_index','incompatible_transcript_model'} <= {w['code'] for w in data['warnings']}


def test_transcript_only_video_and_grounded_answer(settings, audio_media, transcriber):
    backend = p2.TranscriptBackend()
    backend.fail = True
    with client_for(settings, transcriber, backend=backend) as (client, _):
        vid, job = p2.ingest(client, audio_media)
        assert job['status'] == 'partial'
        data = query(client, vid, query='blue', sources=['transcript'], answer=True)
        assert data['evidence'] and all(e['source']=='transcript' for e in data['evidence'])
        assert data['citations'] and 'visual_inspection_unavailable' in {w['code'] for w in data['warnings']}


class ReverseReranker:
    model = 'reverse-mock'
    def __init__(self):
        self.calls = 0
    async def rank(self, query, documents):
        self.calls += 1
        return [(i, float(i)) for i in reversed(range(len(documents)))]


def test_rerank_toggle_and_timeout_fallback(settings, audio_media, transcriber):
    settings.rerank_enabled = True
    reranker = ReverseReranker()
    with client_for(settings, transcriber, reranker=reranker) as (client, _):
        vid, _ = p2.ingest(client, audio_media)
        query(client, vid, rerank=False)
        assert reranker.calls == 0
        data = query(client, vid, rerank=True, queries=['red concept'])
        assert reranker.calls == 1
        assert data['execution']['searches'][-1]['rerank']['status'] == 'applied'
        assert 'rerank_does_not_cover_all_candidates' in {w['code'] for w in data['warnings']}
        async def blocked(*args):
            await asyncio.sleep(.1)
        reranker.rank = blocked
        settings.rerank_timeout_s = .005
        fallback = query(client, vid, rerank=True)
        assert fallback['evidence']
        assert fallback['execution']['searches'][-1]['rerank']['status'] == 'fallback_rrf'
        assert 'rerank_failed_fallback_rrf' in {w['code'] for w in fallback['warnings']}


def test_joint_spoken_concept_ppt_conflict_and_safe_links(settings, audio_media, transcriber):
    models = Models()
    models.conflict = True
    with client_for(settings, transcriber, models=models) as (client, _):
        vid, _ = p2.ingest(client, audio_media)
        with patch.object(FrameStore, '_latest_segment_video_data_uri', side_effect=AssertionError('global video leak')):
            data = query(client, vid, query='blue', sources=['transcript'], answer=True)
        assert {'transcript','frame'} <= {e['source'] for e in data['evidence']}
        assert models.inspections and '冲突' in data['answer']
        assert 'speech_visual_conflict' in {w['code'] for w in data['warnings']}
        available = {e['evidence_id']:e for e in data['evidence']}
        assert all(c['evidence_id'] in available and c['video_id']==vid for c in data['citations'])
        assert all(c['start_t']==available[c['evidence_id']]['start_t'] for c in data['citations'])


def test_invalid_grounded_answer_fails_closed_and_empty_search(settings, audio_media, transcriber):
    models = Models()
    with client_for(settings, transcriber, models=models) as (client, _):
        vid, _ = p2.ingest(client, audio_media)
        models.invalid_id = True
        data = query(client, vid, answer=True)
        assert not data['citations'] and 'answer_validation_failed' in {w['code'] for w in data['warnings']}
        assert 'ev_'+'f'*32 not in data['answer']
        empty = query(client, vid, min_time=20, max_time=21, answer=True)
        assert empty['found'] is False and not empty['evidence'] and not empty['citations']


@pytest.mark.parametrize('options', [{'sources':['bad']}, {'queries':[' ']}, {'queries':['x'*4001]},
                                   {'fusion':'bad'}, {'rerank_api_key':'secret'}, {'rerank_endpoint':'https://other'},
                                   {'min_time': 'NaN'}, {'queries':['a','b','c','d']}])
def test_http_rejects_invalid_settings_and_resources(settings, audio_media, transcriber, options):
    with client_for(settings, transcriber) as (client, _):
        vid, _ = p2.ingest(client, audio_media)
        assert client.post(f'/api/videos/{vid}/search', json={'query':'red', **options}).status_code == 422


def test_ws_nested_transcript_anchor_and_validated_final(settings, audio_media, transcriber):
    models = Models()
    models.plans = [
        {'action':'search','queries':[{'q':'blue','inspect_k':2,'threshold':0}], 'sources':['frame','transcript'],
         'time_range':{'mode':'relative','anchor':{'query':'blue','sources':['transcript'], 'inspect_k':1,'after_seconds':.2}}},
        {'action':'answer','response':'Unverified model draft.'},
    ]
    with client_for(settings, transcriber, models=models) as (client, _):
        vid, _ = p2.ingest(client, audio_media)
        with client.websocket_connect('/ws/agent') as ws:
            ws.receive_json()
            ws.send_json({'type':'tool','name':'retrieve','video_id':vid,'args':{'question':'老师说的blue概念与PPT是什么？'}})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event['type'] in {'final','error'}:
                    assert event['type']=='final', event
                    break
        result = events[-1]['result']
        assert result['evidence'] and result['citations']
        assert 'Unverified model draft' not in json.dumps(events)
        assert 'transcript_anchor_is_text_only' in {w['code'] for w in result['warnings']}
        assert 'data:image' not in json.dumps(events)
        assert all(e['video_id']==vid for e in result['evidence'])


def test_request_contextvar_isolation():
    class Scope:
        def __init__(self, name): self.name=name
        async def search(self, *args, **kwargs):
            await asyncio.sleep(.01)
            return [self.name]
    async def request(name):
        token = active_search.set(Scope(name))
        try:
            return await hybrid_event_frame_search('same query')
        finally:
            active_search.reset(token)
    async def run():
        return await asyncio.gather(request('a'),request('b'))
    assert asyncio.run(run()) == [['a'],['b']]
    assert active_search.get() is None


def test_visual_reference_and_direct_inspection_stay_scoped(settings, audio_media, transcriber):
    models = Models()
    models.plans = [
        {'action':'search','queries':[{'q':'red','inspect_k':0,'threshold':0}], 'sources':['frame']},
        {'action':'search','queries':[{'q':'red','inspect_k':0,'threshold':0}], 'visual_ref':{'turn_idx':1,'result_idx':0}},
        {'action':'inspect','prompt':'Check this slide','ref':{'turn_idx':1,'result_idx':0},'max_frames':2},
        {'action':'answer','response':'Unvalidated draft.'},
    ]
    with client_for(settings, transcriber, models=models) as (client, _):
        vid, _ = p2.ingest(client, audio_media)
        with client.websocket_connect('/ws/agent') as ws:
            ws.receive_json()
            ws.send_json({'type':'tool','name':'retrieve','video_id':vid,'args':{'question':'red slide'}})
            while True:
                event = ws.receive_json()
                if event['type'] in {'error','final'}:
                    assert event['type']=='final', event
                    break
        assert event['result']['citations'] and models.inspections
        assert all(e['video_id']==vid and e['source']=='frame' for e in event['result']['evidence'])


def test_transcript_search_version_is_pinned_during_request(settings, audio_media, transcriber):
    with client_for(settings, transcriber) as (client, _):
        vid, _ = p2.ingest(client, audio_media)
        async def switch_and_search():
            async with client.app.state.indexing.retrieval_context(vid) as scope:
                old = scope.versions['transcript']
                new = 'f'*64
                with scope.transcript.connect(write=True) as conn:
                    conn.execute('INSERT INTO transcript_versions SELECT ?,video_id,transcription_key,source_sha256,asr_fingerprint,chunk_config,embedding_fingerprint,dim,language,status,metadata,created_at FROM transcript_versions WHERE version=?', (new,old))
                    conn.execute('INSERT INTO transcript_chunks SELECT ?,doc_id,start_t,end_t,text,segment_ids,embedding FROM transcript_chunks WHERE version=?', (new,old))
                    conn.execute('UPDATE transcript_heads SET search_version=? WHERE video_id=?', (new,vid))
                hits = await scope.search('red', sources=['transcript'])
                assert hits and all(h['index_version']==old for h in hits)
                assert scope.transcript.state()['search_version']==new
        client.portal.call(switch_and_search)


def test_query_dimension_failure_is_visible(settings, audio_media, transcriber):
    backend = p2.TranscriptBackend()
    with client_for(settings, transcriber, backend=backend) as (client, _):
        vid, _ = p2.ingest(client, audio_media)
        backend.embed_text = lambda *a, **kw: ([1.,0.],2)
        data = query(client, vid, sources=['transcript'])
        assert not data['evidence'] and any(w['source']=='transcript' for w in data['warnings'])


def test_missing_representative_frame_has_no_broken_image_url(settings, audio_media, transcriber):
    with client_for(settings, transcriber) as (client, _):
        vid, _ = p2.ingest(client, audio_media)
        row = client.app.state.indexing.registry.get(vid)
        path = client.app.state.indexing.registry.asset(vid, f"memory/{row['active_generation']}.sqlite3")
        import sqlite3
        with sqlite3.connect(path) as conn:
            conn.execute('DELETE FROM frames')
        data = query(client, vid, sources=['event'])
        assert data['evidence'] and all(e['image_url'] is None for e in data['evidence'])
        assert 'representative_frame_missing' in {w['code'] for w in data['warnings']}


def test_no_text_rerank_and_http_auth_error_fallback(settings):
    frame = evidence('frame')
    mock = MockReranker([])
    result, info = asyncio.run(rerank_text([frame], query='q', reranker=mock, limit=5, timeout=1, text_limit=100))
    assert not mock.calls and info['status']=='no_text_candidates' and result==[frame]
    settings.rerank_api_key='test-key'
    adapter = CohereReranker(settings, transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    # E3: provider calls go through the ModelGateway, which classifies the HTTP error (not retryable, not billed).
    from vam.gateway import ProviderError
    with pytest.raises(ProviderError) as failure:
        asyncio.run(adapter.rank('q',['text']))
    assert failure.value.status == 401 and not failure.value.retryable and not failure.value.sent
    assert isinstance(failure.value.__cause__, httpx.HTTPStatusError)


def test_teacher_concept_and_formula_slide_end_to_end(settings, transcriber, tmp_path):
    image = Image.new('RGB', (640,360), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((30,80), 'Attention = softmax(Q K^T / sqrt(d)) V', fill='black', font_size=24)
    slide = tmp_path/'formula.png'
    image.save(slide)
    video = tmp_path/'formula.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-loop','1','-i',str(slide),
                    '-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','2',
                    '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-y',str(video)], check=True,timeout=30)
    transcriber.segments = [{'start':.1,'end':1.7,'text':'Attention uses softmax of Q K over square root of d, times V.'}]
    models = Models()
    models.inspection_text = 'The PPT displays Attention = softmax(Q K^T / sqrt(d)) V; the quoted speech describes the same formula.'
    with client_for(settings,transcriber,models=models) as (client,_):
        uploaded=client.post('/api/videos',files={'video':('formula.mp4',video.read_bytes(),'video/mp4')}).json()
        assert p2.terminal(client,uploaded['job_id'])['status']=='succeeded'
        data=query(client,uploaded['video_id'],query='Attention softmax formula',sources=['frame','transcript'],answer=True)
        assert {'frame','transcript'} <= {e['source'] for e in data['evidence']}
        assert data['citations'] and models.inspections
        assert any('softmax' in json.dumps(call) for call in models.inspections)
        assert any(e['image_url'] and client.get(e['image_url']).status_code==200 for e in data['evidence'] if e['source']=='frame')


def test_planner_cannot_create_summary_as_retrieval_fallback(settings,audio_media,transcriber):
    models=Models()
    models.plans=[{'action':'summarize','time_range':{'min':0,'max':2},'granularity_seconds':30,'prompt':'auto summary'},
                  {'action':'search','queries':[{'q':'red','inspect_k':0}], 'sources':['frame']},
                  {'action':'answer','response':'draft'}]
    with client_for(settings,transcriber,models=models) as (client,_):
        vid,_=p2.ingest(client,audio_media)
        with client.websocket_connect('/ws/agent') as ws:
            ws.receive_json()
            ws.send_json({'type':'tool','name':'retrieve','video_id':vid,'args':{'question':'overview'}})
            while True:
                event=ws.receive_json()
                if event['type'] in {'final','error'}:
                    assert event['type']=='final',event
                    break
        assert client.get(f'/api/videos/{vid}/memory-documents?kind=summary').json()['total']==0
        assert 'summary_creation_disabled_during_retrieval' in {w['code'] for w in event['result']['warnings']}
