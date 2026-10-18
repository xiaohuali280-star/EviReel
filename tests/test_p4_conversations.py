"""P4 real SQLite/HTTP/WS/media contracts with explicit deterministic model mocks."""
import asyncio
import json
from pathlib import Path
import sqlite3
import time
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import test_p2_transcripts as p2
import test_p3_retrieval as p3
from vam import prompts
from vam.config import Settings
from vam.memory.context_manager import ContextManager, ConversationContext, SUMMARY_SYSTEM, RESOLVE_SYSTEM, encoded_size
from vam.memory.conversation import ConversationStore
from vam.protocol import VideoAgentRequest
from vam.server.app import create_app
from vam.storage.database import Database
from vam.video_registry import DomainError, VideoRegistry

settings, media, audio_media, transcriber = p2.settings, p2.media, p2.audio_media, p2.transcriber


def uid(prefix):
    return prefix + '_' + uuid.uuid4().hex


def register(registry):
    vid = uid('v')
    registry.directory(vid).mkdir()
    registry.register(vid, 'fixture.mp4', 'source.mp4', 100, 120)
    return vid


@pytest.fixture
def durable(tmp_path):
    registry = VideoRegistry(tmp_path)
    return ConversationStore(registry), register(registry), register(registry)


def completed(store, cid, video, question='attention formula', answer='formula', evidence=True):
    rid = uid('r')
    store.accept(cid, rid, video, {'text': question}, question, 10)
    item = p3.evidence('formula', video=video, version='old')
    event = {'type': 'final', 'text': answer, 'result': {'answer': answer,
             'evidence': [item] if evidence else [], 'citations': [{'evidence_id': item['evidence_id']}] if evidence else []}}
    store.finish(cid, rid, event)
    return rid


def test_store_atomic_idempotency_isolation_and_pagination(durable):
    store, a, b = durable
    cid = store.create(a)['conversation_id']
    rid = uid('r')
    assert store.accept(cid, rid, a, {'text': 'question'}, 'question', 2)
    assert not store.accept(cid, rid, a, {'text': 'question'}, 'question', 2)
    for other_rid, other_video, payload, code in ((rid, a, {'text': 'changed'}, 'request_conflict'),
            (uid('r'), a, {}, 'conversation_busy'), (uid('r'), b, {}, 'conversation_video_mismatch')):
        with pytest.raises(DomainError) as error:
            store.accept(cid, other_rid, other_video, payload, 'x', 2)
        assert error.value.code == code
    with pytest.raises(DomainError):
        store.delete(cid)
    event = {'type': 'final', 'text': 'answer', 'result': {'citations': [], 'evidence': []}}
    assert store.finish(cid, rid, event)
    assert not store.finish(cid, rid, event)
    store.fail(cid, rid, 'late_error')
    assert store.request(cid, rid)['status'] == 'completed'
    page = store.messages(cid, limit=1)
    assert len(page['items']) == 1 and page['next_after']
    assert store.messages(cid, after=page['next_after'])['items'][0]['content'] == 'answer'
    assert store.list(b)['items'] == []
    store.delete(cid)
    with store.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM conversation_requests').fetchone()[0] == 0


def test_capacity_and_restart_interrupt_preserve_user_and_finished(durable):
    store, a, b = durable
    cid, other = store.create(a)['conversation_id'], store.create(b)['conversation_id']
    done = completed(store, cid, a)
    rid = uid('r')
    store.accept(cid, rid, a, {}, 'pending', 1)
    with pytest.raises(DomainError, match='capacity'):
        store.accept(other, uid('r'), b, {}, 'other', 1)
    restarted = ConversationStore(VideoRegistry(store.registry.root))
    restarted.recover()
    assert restarted.request(cid, rid)['status'] == 'interrupted'
    assert restarted.request(cid, done)['status'] == 'completed'
    assert [m['role'] for m in restarted.messages(cid)['items']] == ['user', 'assistant', 'user']
    assert len(restarted.turns(cid, limit=6)) == 1


@pytest.mark.parametrize('version', [1, 2])
def test_registry_migration_preserves_old_video(tmp_path, version):
    path = tmp_path/'registry.sqlite3'
    migrations = Path(__file__).parents[1]/'vam/storage/migrations'
    with sqlite3.connect(path) as conn:
        conn.executescript((migrations/'001_registry.sql').read_text())
        if version == 2:
            conn.executescript((migrations/'002_transcripts.sql').read_text())
        conn.execute("INSERT INTO videos(video_id,original_name,source_path,size_bytes,duration,created_at) VALUES('old','old.mp4','source.mp4',1,10,0)")
        conn.execute(f'PRAGMA user_version={version}')
    db = Database(path)
    with db.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 3
        assert conn.execute('SELECT original_name FROM videos').fetchone()[0] == 'old.mp4'
        assert conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0] == 0


class MemoryModel:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError('explicit summary mock failure')
        return 'Attention formula followed by code; historical summary.', {}


def test_incremental_summary_boundaries_and_complete_turns(durable):
    store, a, _ = durable
    cid = store.create(a)['conversation_id']
    for i in range(9):
        completed(store, cid, a, question=f'question {i}', answer=f'answer {i}')
    cfg = Settings(_env_file=None, conversation_recent_turns=6)
    model = MemoryModel()
    manager = ContextManager(store, cfg, model)
    context = asyncio.run(manager.build(cid))
    data = json.loads(context.text)
    assert len(data['turns']) == 6 and data['turns'][0]['question'] == 'question 3'
    assert store.get(cid)['summary_through'] == 6
    assert model.calls[0][0]['content'] == SUMMARY_SYSTEM
    asyncio.run(manager.build(cid))
    assert len(model.calls) == 1
    completed(store, cid, a, question='new question')
    asyncio.run(manager.build(cid))
    assert len(model.calls) == 2
    delta = json.loads(model.calls[1][-1]['content'])
    assert [t['question'] for t in delta['turns']] == ['question 3']
    assert delta['summary'].startswith('Attention')
    assert len(store.messages(cid, limit=200)['items']) == 20


@pytest.mark.parametrize('fail', [False, True])
def test_hard_context_budgets_and_summary_failure(durable, fail):
    store, a, _ = durable
    cid = store.create(a)['conversation_id']
    for i in range(12):
        completed(store, cid, a, question=f'{i}公式'*1000, answer='中文'*4000)
    cfg = Settings(_env_file=None, conversation_recent_turns=2, conversation_context_chars=1800,
                   conversation_context_tokens=1600, conversation_message_chars=100)
    model = MemoryModel(fail)
    manager = ContextManager(store, cfg, model)
    context = asyncio.run(manager.build(cid, 'question'))
    assert len(context.text + 'question' + ' '*512) <= cfg.conversation_context_chars
    assert encoded_size(context.text + 'question' + ' '*512) <= cfg.conversation_context_tokens
    for call in model.calls:
        assert manager.fits(call[-1]['content'])
    count = len(model.calls)
    asyncio.run(manager.build(cid, 'question'))
    if fail:
        assert len(model.calls) == count
        assert 'context_summary_failed' in context.warnings
    assert len(store.messages(cid, limit=200)['items']) == 24
    assert all(len(t['question']) <= 100 and len(t['answer']) <= 100 for t in json.loads(context.text)['turns'])


@pytest.mark.parametrize('mode', ['valid', 'unknown', 'multiple', 'ambiguous', 'failure', 'no_anchor'])
def test_reference_resolution_is_structured_and_conservative(durable, mode):
    store, a, _ = durable
    cid = store.create(a)['conversation_id']
    completed(store, cid, a)
    class Resolver:
        async def chat(self, messages, **kwargs):
            if mode == 'failure':
                raise RuntimeError('outage')
            assert messages[0]['content'] == RESOLVE_SYSTEM
            data = json.loads(messages[-1]['content'])
            anchor = data['context']['anchors'][0]['evidence_id']
            return json.dumps({'rewritten_question': '注意力公式之后的代码实现在哪里',
                               'anchor_ids': [anchor] if mode not in {'unknown', 'multiple'} else (['unknown'] if mode == 'unknown' else [anchor, anchor]),
                               'needs_clarification': mode == 'ambiguous'}), {}
    manager = ContextManager(store, Settings(_env_file=None), Resolver())
    context = asyncio.run(manager.build(cid))
    if mode == 'no_anchor':
        context = ConversationContext('{}', [], [])
    result = asyncio.run(manager.resolve('这个公式后面有代码实现吗', context))
    assert result['original_question'] == '这个公式后面有代码实现吗'
    assert result['needs_clarification'] == (mode != 'valid')
    if mode == 'valid':
        assert result['advisory_time_range'] == {'start_t': 0, 'end_t': 0}
        assert result['anchor_is_historical']


def send_question(ws, video, cid, rid, text='注意力公式在哪里'):
    ws.send_json({'type': 'text', 'video_id': video, 'conversation_id': cid, 'request_id': rid, 'text': text})


def receive_final(ws):
    events = []
    while True:
        event = ws.receive_json()
        assert event['type'] != 'error', event
        events.append(event)
        if event['type'] == 'final':
            return events


class FollowupModels(p3.Models):
    def __init__(self):
        super().__init__()
        self.router_contexts, self.planner_contexts = [], []
        self.resolutions = []

    async def chat(self, client, messages, **kwargs):
        system = messages[0]['content']
        if system == RESOLVE_SYSTEM:
            data = json.loads(messages[-1]['content'])
            self.resolutions.append(data)
            return json.dumps({'rewritten_question': '注意力公式之后的 blue code 实现在哪里',
                               'anchor_ids': [data['context']['anchors'][0]['evidence_id']],
                               'needs_clarification': False}), {}
        if system == SUMMARY_SYSTEM:
            return 'Attention formula and subsequent code.', {}
        if system == prompts.ws_agent_system():
            self.router_contexts.append(messages)
            if 'blue code' in messages[-1]['content']:
                return json.dumps({'type': 'tool', 'name': 'retrieve', 'args': {'question': 'untracked router rewrite'}}), {}
        if system == prompts.agent_planner_system():
            self.planner_contexts.append(messages)
        return await super().chat(client, messages, **kwargs)


def test_formula_code_history_reconnect_idempotency_and_video_isolation(settings, audio_media, transcriber):
    models = FollowupModels()
    with p3.client_for(settings, transcriber, models=models) as (client, _):
        a, _ = p2.ingest(client, audio_media)
        b, _ = p2.ingest(client, audio_media)
        cid = client.post('/api/conversations', json={'video_id': a}).json()['conversation_id']
        rid = uid('r')
        with client.websocket_connect('/ws/agent') as ws:
            ws.receive_json()
            send_question(ws, a, cid, rid)
            first = receive_final(ws)[-1]
            assert first['result']['citations']
        with client.websocket_connect('/ws/agent') as ws:
            ws.receive_json()
            send_question(ws, a, cid, rid)
            repeated = receive_final(ws)
            assert repeated[0]['replayed']
            assert repeated[-1]['result']['citations'] == first['result']['citations']
            assert len(models.router_contexts) == 1
            send_question(ws, a, cid, rid, 'different text')
            assert ws.receive_json()['code'] == 'request_conflict'
            send_question(ws, b, cid, uid('r'))
            assert ws.receive_json()['code'] == 'conversation_video_mismatch'
            followup_id = uid('r')
            send_question(ws, a, cid, followup_id, '这个公式后面有代码实现吗')
            followup = receive_final(ws)
            assert all(e['protocol_version'] == 2 and e['conversation_id'] == cid and e['request_id'] == followup_id and e['video_id'] == a for e in followup)
            final = followup[-1]
            assert 'blue code' in final['resolution']['rewritten_question']
            assert all(e['video_id'] == a for e in final['result']['evidence'])
        assert len(models.router_contexts) == 2
        router_history = models.router_contexts[-1][1]['content']
        assert '注意力公式在哪里' in router_history and first['result']['citations'][0]['evidence_id'] in router_history
        assert '注意力公式在哪里' in models.planner_contexts[-1][-1]['content']
        assert 'blue code' in models.planner_contexts[-1][-1]['content']
        assert 'untracked router rewrite' not in models.planner_contexts[-1][-1]['content']
        assert 'advisory' in router_history
        request = client.get(f'/api/conversations/{cid}/requests/{followup_id}').json()
        assert request['status'] == 'completed' and request['original_text'] == '这个公式后面有代码实现吗'
        messages = client.get(f'/api/conversations/{cid}/messages').json()['items']
        assert len(messages) == 4
        assert client.get('/api/conversations', params={'video_id': b}).json()['total'] == 0
        assert client.get('/api/conversations', params={'video_id': a, 'limit': 201}).status_code == 422
        # Rebuild changes current IDs; historical evidence remains the old snapshot.
        client.app.state.indexing.registry.update_video(a, active_generation='f'*32)
        historical = client.get(f'/api/conversations/{cid}/requests/{rid}').json()['result']
        old_visual = [e for e in historical['result']['evidence'] if e['source'] != 'transcript']
        assert old_visual and all(e['stale_evidence'] for e in old_visual)
        assert historical['result']['citations'] == first['result']['citations']
        client.app.state.indexing.registry.update_video(a, active_generation=first['index_version'])
        assert client.delete(f'/api/conversations/{cid}').status_code == 204


def wait_request(client, cid, rid):
    for _ in range(200):
        row = client.get(f'/api/conversations/{cid}/requests/{rid}').json()
        if row['status'] not in {'accepted', 'running'}:
            return row
        time.sleep(.01)
    raise AssertionError('Request did not finish')


def test_disconnect_busy_delete_timeout_and_restart(settings):
    from vam import conversation_service as module
    calls = []
    async def slow(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(.2)
        yield {'type': 'final', 'text': 'durable answer', 'result': {'evidence': [], 'citations': []}}
    with patch.object(module, 'stream_agent_response', slow):
        with TestClient(create_app(settings=settings, backend=p2.TranscriptBackend())) as client:
            vid = register(client.app.state.indexing.registry)
            # Supply a valid search scope without models; actual retrieval covered above.
            from contextlib import asynccontextmanager
            from types import SimpleNamespace
            @asynccontextmanager
            async def scoped(video_id):
                yield SimpleNamespace(visual=object(), backend=None, row={'active_generation': None})
            client.app.state.indexing.retrieval_context = scoped
            cid = client.post('/api/conversations', json={'video_id': vid}).json()['conversation_id']
            rid = uid('r')
            with client.websocket_connect('/ws/agent') as ws:
                ws.receive_json()
                send_question(ws, vid, cid, rid, 'where code')
                assert ws.receive_json()['type'] == 'accepted'
                send_question(ws, vid, cid, uid('r'), 'next question')
                while True:
                    event = ws.receive_json()
                    if event['type'] == 'error':
                        assert event['code'] == 'conversation_busy'
                        break
                assert client.delete(f'/api/conversations/{cid}').status_code == 409
                assert client.delete(f'/api/videos/{vid}').status_code == 409
            completed_row = wait_request(client, cid, rid)
            assert completed_row['status'] == 'completed' and len(calls) == 1
            # Already persisted result is replayed even if no socket received original final.
            with client.websocket_connect('/ws/agent') as ws:
                ws.receive_json()
                send_question(ws, vid, cid, rid, 'where code')
                assert receive_final(ws)[-1]['text'] == 'durable answer'
            assert len(calls) == 1
            settings.conversation_request_timeout_s = .02
            timed = uid('r')
            with client.websocket_connect('/ws/agent') as ws:
                ws.receive_json()
                send_question(ws, vid, cid, timed, 'timeout code')
                while ws.receive_json()['type'] != 'error':
                    pass
            assert wait_request(client, cid, timed)['error_code'] == 'request_timeout'
            assert len(client.get(f'/api/conversations/{cid}/messages').json()['items']) == 3
            settings.conversation_request_timeout_s = 20
            interrupted = uid('r')
            with client.websocket_connect('/ws/agent') as ws:
                ws.receive_json()
                send_question(ws, vid, cid, interrupted, 'pending code')
                assert ws.receive_json()['type'] == 'accepted'
        with TestClient(create_app(settings=settings, backend=p2.TranscriptBackend())) as client:
            assert client.get(f'/api/conversations/{cid}/requests/{interrupted}').json()['status'] == 'interrupted'
            assert client.get(f'/api/conversations/{cid}/requests/{rid}').json()['result']['text'] == 'durable answer'
            assert client.get(f'/api/conversations/{cid}').json()['video_id'] == vid


@pytest.mark.parametrize('payload', [
    {'conversation_id': 'c_'+'a'*32}, {'request_id': 'r_'+'a'*32},
    {'conversation_id': '../other', 'request_id': 'r_'+'a'*32}, {'text': ''},
])
def test_protocol_rejects_partial_or_invalid_ids(payload):
    with pytest.raises(ValueError):
        VideoAgentRequest.model_validate({'type': 'text', 'video_id': 'v_'+'a'*32, 'text': 'question', **payload})


def test_reset_creates_new_persistent_conversation_and_no_guess(settings):
    with TestClient(create_app(settings=settings, backend=p2.TranscriptBackend())) as client:
        vid = register(client.app.state.indexing.registry)
        with client.websocket_connect('/ws/agent') as ws:
            ws.receive_json()
            ws.send_json({'type': 'reset', 'video_id': vid})
            first = ws.receive_json()
            ws.send_json({'type': 'reset', 'video_id': vid})
            second = ws.receive_json()
            assert first['conversation_id'] != second['conversation_id']
            send_question(ws, vid, second['conversation_id'], uid('r'), '这个公式后面有代码吗')
            result = receive_final(ws)[-1]
            assert result['result']['needs_clarification'] and result['result']['citations'] == []
        assert client.get('/api/conversations', params={'video_id': vid}).json()['total'] == 2


def test_failed_request_does_not_save_partial_answer_and_replays_error(settings):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from vam import conversation_service as module
    calls = []
    async def broken(**kwargs):
        calls.append(1)
        yield {'type': 'plan', 'text': 'partial progress'}
        raise RuntimeError('explicit model mock failure')
    @asynccontextmanager
    async def scoped(video_id):
        yield SimpleNamespace(visual=object(), backend=None, row={'active_generation': None})
    with patch.object(module, 'stream_agent_response', broken):
        with TestClient(create_app(settings=settings, backend=p2.TranscriptBackend())) as client:
            vid = register(client.app.state.indexing.registry)
            client.app.state.indexing.retrieval_context = scoped
            cid = client.post('/api/conversations', json={'video_id': vid}).json()['conversation_id']
            rid = uid('r')
            for _ in range(2):
                with client.websocket_connect('/ws/agent') as ws:
                    ws.receive_json()
                    send_question(ws, vid, cid, rid, 'find code')
                    while True:
                        event = ws.receive_json()
                        assert event['type'] != 'final'
                        if event['type'] == 'error':
                            assert event['code'] == 'agent_failed'
                            break
            assert len(calls) == 1
            messages = client.get(f'/api/conversations/{cid}/messages').json()['items']
            assert len(messages) == 1 and messages[0]['role'] == 'user' and messages[0]['status'] == 'failed'
            assert not client.app.state.indexing.leases
            assert client.delete(f'/api/videos/{vid}').status_code == 204
            assert client.get(f'/api/conversations/{cid}').status_code == 404


def test_oversize_question_rejected_before_acceptance_and_subscription_bounded(settings):
    settings.conversation_message_chars = 100
    with TestClient(create_app(settings=settings, backend=p2.TranscriptBackend())) as client:
        vid = register(client.app.state.indexing.registry)
        cid = client.post('/api/conversations', json={'video_id': vid}).json()['conversation_id']
        with client.websocket_connect('/ws/agent') as ws:
            ws.receive_json()
            send_question(ws, vid, cid, uid('r'), 'x'*101)
            assert ws.receive_json()['code'] == 'question_too_large'
            assert client.get(f'/api/conversations/{cid}/messages').json()['items'] == []
            for _ in range(3):
                send_question(ws, vid, cid, uid('r'), '这个公式后面呢')
                assert receive_final(ws)[-1]['result']['needs_clarification']
            assert not client.app.state.conversations.subscribers


def test_clarification_keeps_last_relevant_anchor(durable):
    store, a, _ = durable
    cid = store.create(a)['conversation_id']
    completed(store, cid, a)
    completed(store, cid, a, question='which one', answer='please clarify', evidence=False)
    context = asyncio.run(ContextManager(store, Settings(_env_file=None), MemoryModel()).build(cid))
    assert context.anchors and context.anchors[0]['text'] == ''
    assert len(json.loads(context.text)['turns']) == 2


def test_final_is_committed_before_transport_failure(settings):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from starlette.websockets import WebSocketDisconnect
    from vam import conversation_service as module
    from vam.server.ws import agent_chat as socket_module
    checked = []
    async def result(**kwargs):
        yield {'type': 'final', 'text': 'committed before send', 'result': {'evidence': [], 'citations': []}}
    @asynccontextmanager
    async def scoped(video_id):
        yield SimpleNamespace(visual=object(), backend=None, row={'active_generation': None})
    original_send = socket_module._safe_send_text
    async def failing_send(websocket, event):
        if event.get('type') == 'final':
            row = websocket.app.state.conversations.store.request(event['conversation_id'], event['request_id'])
            assert row['status'] == 'completed' and row['result']['text'] == event['text']
            checked.append(row)
            return False
        return await original_send(websocket, event)
    with patch.object(module, 'stream_agent_response', result), patch.object(socket_module, '_safe_send_text', failing_send):
        with TestClient(create_app(settings=settings, backend=p2.TranscriptBackend())) as client:
            vid = register(client.app.state.indexing.registry)
            client.app.state.indexing.retrieval_context = scoped
            cid = client.post('/api/conversations', json={'video_id': vid}).json()['conversation_id']
            rid = uid('r')
            with client.websocket_connect('/ws/agent') as ws:
                ws.receive_json()
                send_question(ws, vid, cid, rid, 'find code')
                with pytest.raises(WebSocketDisconnect):
                    while True:
                        ws.receive_json()
            assert checked
            assert client.get(f'/api/conversations/{cid}/requests/{rid}').json()['result']['text'] == 'committed before send'
