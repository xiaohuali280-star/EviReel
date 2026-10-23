"""Real HTTP/WS/SQLite/media with explicit synthetic ASR and model adapters."""
import json
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

import test_p2_transcripts as p2
from test_p5_study import rid, study_model, study_terminal
from vam.server.app import create_app
from vam.chat_text import parse_answer, text_history
from vam.retrieval.evidence import validate_answer
from test_p3_retrieval import evidence
from vam.memory.context_manager import ConversationContext

settings, media, audio_media, transcriber = p2.settings, p2.media, p2.audio_media, p2.transcriber


async def answer_model(messages):
    data = json.loads(messages[-1]['content'])
    return json.dumps({'claims': [{'text': '文稿中的课程内容。', 'evidence_ids': [e['evidence_id']]} for e in data['sources'][:3]], 'warnings': []})


@pytest.fixture
def client(settings, transcriber):
    settings.study_auto_generate = False
    backend = p2.TranscriptBackend()
    backend.fail_document = True

    async def no_vision(**kwargs):
        raise RuntimeError('explicit visual outage')

    with TestClient(create_app(settings=settings, backend=backend, transcriber=transcriber,
                               indexer=no_vision, study_model=study_model)) as http:
        if hasattr(http.app.state.conversations, 'actions'):
            http.app.state.conversations.actions.model = answer_model
        yield http


def question(client, vid, text='这段视频的主要内容是什么？', **extra):
    cid = client.post('/api/conversations', json={'video_id': vid}).json()['conversation_id']
    payload = {'type': 'text', 'video_id': vid, 'conversation_id': cid, 'request_id': rid(), 'text': text, **extra}
    with client.websocket_connect('/ws/agent') as ws:
        ws.send_json(payload)
        for _ in range(30):
            event = ws.receive_json()
            if event.get('request_id') == payload['request_id'] and event['type'] in ('final', 'error'):
                return payload, event
    raise AssertionError('No terminal event')


def resource(client, vid, item):
    query = urlencode({'owner': item['video_id'], **{k: v for k, v in item.items() if k not in ('video_id', 'label') and v is not None}})
    return client.get(f'/api/videos/{vid}/chat/resource?{query}')


def test_readable_text_enables_grounded_chat_without_embeddings(client, audio_media):
    vid, _ = p2.ingest(client, audio_media)
    detail = client.get(f'/api/videos/{vid}').json()
    assert not detail['capabilities']['vision'] and not detail['capabilities']['transcript_search']
    assert detail['chat']['available'] and detail['chat']['mode'] == 'text'
    payload, event = question(client, vid)
    assert event['type'] == 'final', event
    assert event['result']['material_mode'] == 'transcript'
    assert event['result']['citations'] and event['result']['evidence']
    messages = client.get(f"/api/conversations/{payload['conversation_id']}/messages").json()['items']
    assert len(messages) == 2 and messages[0]['content'] == payload['text']
    assert all(not e['stale_evidence'] for e in messages[1]['result']['result']['evidence'])


@pytest.mark.parametrize('action', ['notes', 'slides', 'transcript'])
def test_actions_return_real_owned_resources(client, audio_media, action):
    vid, _ = p2.ingest(client, audio_media)
    payload, event = question(client, vid, text='生成学习材料', action=action)
    assert event['type'] == 'final', event
    item = event['result']['resources'][0]
    assert item['video_id'] == vid
    if item.get('job_id') and item['kind'] == 'study':
        assert study_terminal(client, vid, item['job_id'])['status'] == 'succeeded'
    response = resource(client, vid, item)
    assert response.status_code == 200, response.text
    state = response.json()
    assert state['status'] == 'ready' and state['links']
    for link in state['links']:
        assert client.get(link['url']).status_code == 200
    other, _ = p2.ingest(client, audio_media, filename='blue-audio.mp4')
    assert resource(client, other, item).status_code in (404, 409)


def test_scoped_question_checks_time_and_clarifies_missing_scope(client, audio_media):
    vid, _ = p2.ingest(client, audio_media)
    _, event = question(client, vid, text='这部分的要点有什么好处？', scope={'start_t': 0, 'end_t': 1, 'version': client.get(f'/api/videos/{vid}').json()['transcript']['text_version']})
    assert event['type'] == 'final' and event['result']['citations']
    _, event = question(client, vid, text='这部分的要点有什么好处？')
    assert event['type'] == 'final' and event['result']['needs_clarification']
    _, event = question(client, vid, scope={'start_t': 100, 'end_t': 200})
    assert event['type'] == 'error' and event['code'] == 'invalid_time_range'


def test_shortcut_replay_is_idempotent_and_retains_action(client, audio_media):
    vid, _ = p2.ingest(client, audio_media)
    payload, first = question(client, vid, text='生成完整笔记', action='notes')
    with client.websocket_connect('/ws/agent') as ws:
        ws.send_json(payload)
        for _ in range(20):
            event = ws.receive_json()
            if event.get('request_id') == payload['request_id'] and event['type'] == 'final':
                break
    assert event['result']['resources'] == first['result']['resources']
    rows = client.get(f"/api/conversations/{payload['conversation_id']}/messages").json()['items']
    assert len(rows) == 2 and rows[0]['action'] == 'notes'
    with client.app.state.indexing.registry.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM study_jobs WHERE video_id=?', (vid,)).fetchone()[0] == 1
    with client.websocket_connect('/ws/agent') as ws:
        ws.send_json(payload | {'action': 'slides'})
        for _ in range(20):
            changed = ws.receive_json()
            if changed.get('type') == 'error':
                break
    assert changed['code'] == 'request_conflict'


def test_transcript_shortcut_can_start_asr_under_its_conversation_lease(client, audio_media):
    service = client.app.state.indexing
    service.settings.asr_enabled = False
    vid, _ = p2.ingest(client, audio_media)
    service.settings.asr_enabled = True
    _, event = question(client, vid, action='transcript')
    assert event['type'] == 'final', event
    item = event['result']['resources'][0]
    assert item['job_id']
    p2.terminal(client, item['job_id'])
    response = resource(client, vid, item)
    assert response.status_code == 200 and response.json()['status'] == 'ready'
    assert not service.leases


def test_stale_context_and_manuscript_download_are_rejected(client, audio_media):
    vid, _ = p2.ingest(client, audio_media)
    _, event = question(client, vid, scope={'start_t': 0, 'end_t': 1, 'version': 'f' * 64})
    assert event['type'] == 'error' and event['code'] == 'chat_context_stale'
    result = client.get(f'/api/videos/{vid}/transcripts/export?format=txt&version=' + 'f' * 64)
    assert result.status_code == 409 and result.json()['error']['code'] == 'chat_context_stale'


def test_model_cannot_invent_evidence(client, audio_media):
    vid, _ = p2.ingest(client, audio_media)

    calls = []

    async def bad(messages):
        calls.append(messages)
        return {'claims': [{'text': 'PRIVATE bad answer', 'evidence_ids': ['foreign']}], 'warnings': []}

    client.app.state.conversations.actions.model = bad
    payload, event = question(client, vid)
    assert event['type'] == 'error' and event['code'] == 'chat_invalid_output'
    assert 'PRIVATE' not in json.dumps(event)
    rows = client.get(f"/api/conversations/{payload['conversation_id']}/messages").json()['items']
    assert len(rows) == 1 and rows[0]['status'] == 'failed'
    assert len(calls) == 2


def test_text_answer_accepts_course_workload_but_not_navigation_or_urls():
    source = evidence('课程建议每周投入5小时。')
    payload = {'claims': [{'text': '课程建议每周投入5小时。', 'evidence_ids': [source['evidence_id']]}], 'warnings': []}
    # Reproduces the real validation conflict; strict retrieval keeps its policy.
    with pytest.raises(ValueError):
        validate_answer(payload, [source])
    payload['claims'][0]['evidence_ids'] = ['s1']
    aliases = {'s1': source['evidence_id']}
    text, citations, _ = parse_answer('```json\n' + json.dumps(payload) + '\n```', aliases, [source])
    assert '5小时' in text and citations[0]['evidence_id'] == source['evidence_id']
    for bad in ('跳转到00:30', 'https://example.com', 'ev_foreign'):
        payload['claims'][0]['text'] = bad
        with pytest.raises(ValueError):
            parse_answer(payload, aliases, [source])


def test_invalid_output_is_repaired_once_with_current_aliases(client, audio_media):
    vid, _ = p2.ingest(client, audio_media)
    calls = []

    async def model(messages):
        calls.append(len(messages))
        data = json.loads(messages[-1]['content'])
        return {'claims': [{'text': '跳转到00:30' if len(calls) == 1 else '文稿中的课程内容。',
                            'evidence_ids': [data['sources'][0]['evidence_id']]}], 'warnings': []}

    client.app.state.conversations.actions.model = model
    _, event = question(client, vid)
    assert event['type'] == 'final' and event['result']['citations']
    assert calls == [2, 3]


def test_resource_shortcuts_do_not_depend_on_conversation_model(client, audio_media):
    vid, _ = p2.ingest(client, audio_media)

    async def forbidden(*args):
        raise AssertionError('Resource shortcuts must not call context models')

    client.app.state.conversations.contexts.build = forbidden
    _, event = question(client, vid, action='notes')
    assert event['type'] == 'final' and event['result']['resources']


def test_text_history_never_supplies_historical_citation_identity():
    old = 'ev_' + 'a' * 32
    context = ConversationContext(json.dumps({'summary': '课程总结', 'turns': [{'question': '问题', 'answer': '课程内容 [' + old + '] (0.00–28.00s)'}],
        'anchors': [{'evidence_id': old, 'start_t': 0, 'end_t': 28}]}), [], [])
    history = text_history(context)
    assert history['turns'][0]['answer'].strip() == '课程内容'
    assert old not in json.dumps(history) and 'anchors' not in history


def test_provider_duplicate_json_is_accepted_only_when_identical():
    source = evidence('课程内容。')
    aliases = {'s1': source['evidence_id']}
    payload = {'claims': [{'text': '课程内容。', 'evidence_ids': ['s1']}], 'warnings': []}
    raw = json.dumps(payload)
    text, citations, _ = parse_answer(raw + '\n```json\n' + raw + '\n```', aliases, [source])
    assert '课程内容' in text and len(citations) == 1
    fields_separated = json.dumps({'claims': payload['claims']}) + '\n' + json.dumps({'warnings': []})
    assert parse_answer(fields_separated, aliases, [source]) == parse_answer(raw, aliases, [source])
    different = json.dumps(payload | {'warnings': ['insufficient_evidence']})
    for tail in ('\n```json\n' + different + '\n```', '\nextra text', '\n' + raw):
        with pytest.raises(ValueError):
            parse_answer(raw + tail, aliases, [source])
