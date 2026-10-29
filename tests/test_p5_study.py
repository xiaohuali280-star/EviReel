"""P5 uses real DB/media/HTTP; model responses below are explicit test fixtures."""
import asyncio
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import time
import uuid
from unittest.mock import AsyncMock, patch
import zipfile

import httpx
from fastapi.testclient import TestClient
import pytest

import test_p2_transcripts as p2
from vam.config import Settings
from vam.indexing.service import IndexingService
from vam.server.app import create_app
from vam.storage.database import Database
from vam.study import StudyService
from vam.study_exports import clock, export_study, manuscript
from vam.video_registry import DomainError

settings, media, audio_media, transcriber = p2.settings, p2.media, p2.audio_media, p2.transcriber


def rid():
    return 'r_' + uuid.uuid4().hex


async def study_model(messages):
    records = json.loads(messages[-1]['content'])
    return {'chapters': [{'title': '视频知识点', 'points': [r['text']], 'source_ids': [r['source_id']]} for r in records]}


def study_terminal(client, vid, jid):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f'/api/videos/{vid}/study/jobs/{jid}').json()
        if job['status'] not in {'queued', 'running'}:
            return job
        time.sleep(.02)
    raise AssertionError('study did not finish')


@pytest.fixture
def client(settings, transcriber):
    settings.study_auto_generate = False
    settings.frontend_dist = str(Path(settings.data_root).parent/'absent-web')
    with patch('vam.llm.openrouter.OpenRouterClient.chat', new_callable=AsyncMock, return_value=('Visible slide.', {})):
        with TestClient(create_app(settings=settings, transcriber=transcriber, backend=p2.TranscriptBackend(), study_model=study_model)) as client:
            yield client


def generate(client, vid):
    response = client.post(f'/api/videos/{vid}/study', json={'request_id': rid()})
    assert response.status_code == 202, response.text
    job = study_terminal(client, vid, response.json()['job_id'])
    assert job['status'] == 'succeeded', job
    return client.get(f'/api/videos/{vid}/study').json()['artifact']


def test_complete_notes_exports_and_trash_restore(client, audio_media):
    vid, _ = p2.ingest(client, audio_media)
    artifact = generate(client, vid)
    assert not artifact['stale']
    content = artifact['content']
    chunks = client.get(f'/api/videos/{vid}/transcripts').json()['items']
    assert len(content['chapters']) == len(chunks) == 2
    assert {e['doc_id'] for e in content['evidence']} == {c['doc_id'] for c in chunks}
    assert [c['start_t'] for c in content['chapters']] == [c['start_t'] for c in chunks]
    aid = artifact['artifact_id']
    for format, prefix in [('md', '# '), ('pdf', '%PDF-'), ('pptx', 'PK')]:
        response = client.get(f'/api/videos/{vid}/study/artifacts/{aid}/export?format={format}')
        assert response.status_code == 200
        assert response.content.startswith(prefix.encode())
        if format == 'pptx':
            with zipfile.ZipFile(BytesIO(response.content)) as archive:
                slides = [n for n in archive.namelist() if n.startswith('ppt/slides/slide') and n.endswith('.xml')]
                assert len(slides) == 2
                assert '红色'.encode() in archive.read(slides[0])
    txt = client.get(f'/api/videos/{vid}/transcripts/export?format=txt').text
    assert '红色 red slide。' in txt and '蓝色 blue vector.' in txt
    srt = client.get(f'/api/videos/{vid}/transcripts/export?format=srt').text
    assert '00:00:00,100 --> 00:00:00,650' in srt
    cid = client.post('/api/conversations', json={'video_id': vid}).json()['conversation_id']
    assert client.post(f'/api/videos/{vid}/trash').status_code == 200
    assert client.get('/api/videos').json()['total'] == 0
    assert client.get('/api/videos?trash=true').json()['items'][0]['video_id'] == vid
    for path in ('content', 'study', 'transcripts', 'frames'):
        assert client.get(f'/api/videos/{vid}/{path}').status_code == 409
    assert client.post('/api/conversations', json={'video_id': vid}).status_code == 409
    assert client.post(f'/api/videos/{vid}/index').status_code == 409
    assert client.post(f'/api/videos/{vid}/restore').status_code == 200
    assert client.get(f'/api/videos/{vid}/study').json()['artifact']['artifact_id'] == aid
    assert client.get(f'/api/conversations/{cid}').status_code == 200
    assert client.delete(f'/api/videos/{vid}').status_code == 204
    with client.app.state.indexing.registry.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM study_artifacts').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM study_jobs').fetchone()[0] == 0


def test_failures_keep_previous_artifact_and_mark_changed_version(client, audio_media):
    vid, _ = p2.ingest(client, audio_media)
    artifact = generate(client, vid)
    async def invalid(messages):
        return {'chapters': [{'title': 'bad', 'points': ['bad'], 'source_ids': ['foreign-source']}]}
    client.app.state.study.model = invalid
    response = client.post(f'/api/videos/{vid}/study', json={'request_id': rid()}).json()
    assert study_terminal(client, vid, response['job_id'])['status'] == 'failed'
    state = client.get(f'/api/videos/{vid}/study').json()
    assert state['artifact']['artifact_id'] == artifact['artifact_id']
    client.app.state.indexing.registry.update_video(vid, active_generation='f' * 32)
    assert client.get(f'/api/videos/{vid}/study').json()['artifact']['stale']
    other, _ = p2.ingest(client, audio_media, filename='blue-audio.mp4')
    assert client.get(f"/api/videos/{other}/study/artifacts/{artifact['artifact_id']}/export").status_code == 404
    assert client.get(f"/api/videos/{other}/study/jobs/{response['job_id']}").status_code == 404


def test_real_http_boundary_persists_auth_failure_and_retains_notes(client, audio_media):
    from test_study_http import client_factory
    vid, _ = p2.ingest(client, audio_media)
    artifact = generate(client, vid)
    service = client.app.state.study
    service.model = None
    service.settings.siliconflow_api_key = 'test-only-not-a-secret'

    def handler(request):
        return httpx.Response(401, json={'error': {'message': 'PRIVATE provider content test-only-not-a-secret'}})

    with patch('httpx.AsyncClient', client_factory(handler)):
        response = client.post(f'/api/videos/{vid}/study', json={'request_id': rid()}).json()
        job = study_terminal(client, vid, response['job_id'])
    assert job['status'] == 'failed' and job['error_code'] == 'study_model_auth_failed'
    state = client.get(f'/api/videos/{vid}/study').json()
    assert state['artifact']['artifact_id'] == artifact['artifact_id']
    assert 'PRIVATE' not in json.dumps(state) and 'test-only' not in json.dumps(state)
    assert not service.indexing.leases


def test_real_http_stream_retry_publishes_complete_artifact(client, audio_media):
    from test_study_http import FragmentedStream, client_factory, sse
    vid, _ = p2.ingest(client, audio_media)
    service = client.app.state.study
    service.model = None
    service.settings.siliconflow_api_key = 'test-only-not-a-secret'
    seen = []

    def handler(request):
        seen.append(request)
        records = json.loads(request.content)['messages'][-1]['content']
        chapters = [{'title': '章节', 'points': [r['text']], 'source_ids': [r['source_id']]} for r in json.loads(records)]
        raw = json.dumps({'chapters': chapters}, ensure_ascii=False)
        first = len(seen) == 1
        stream = FragmentedStream(sse(raw='partial', finish=None, done=False) if first else sse(raw), fail=first)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

    with patch('httpx.AsyncClient', client_factory(handler)):
        artifact = generate(client, vid)
    content = artifact['content']
    assert len(seen) == 2 and len(content['chapters']) == 2
    assert {i for c in content['chapters'] for i in c['source_ids']} == {e['evidence_id'] for e in content['evidence']}
    assert not service.indexing.leases


def test_running_job_idempotency_busy_and_shutdown(client, audio_media):
    vid, _ = p2.ingest(client, audio_media)
    async def wait(messages):
        await asyncio.sleep(60)
    client.app.state.study.model = wait
    request = {'request_id': rid()}
    first = client.post(f'/api/videos/{vid}/study', json=request).json()
    assert client.post(f'/api/videos/{vid}/study', json=request).json()['job_id'] == first['job_id']
    assert client.post(f'/api/videos/{vid}/study', json={'request_id': rid()}).status_code == 409
    assert client.post(f'/api/videos/{vid}/trash').status_code == 409
    assert client.delete(f'/api/videos/{vid}').status_code == 409
    assert client.post(f'/api/videos/{vid}/index').status_code == 409
    client.portal.call(client.app.state.study.close)
    state = client.get(f'/api/videos/{vid}/study').json()
    assert state['job']['status'] == 'interrupted'
    assert not client.app.state.indexing.leases


def test_auto_generation_and_restart(settings, audio_media, transcriber):
    with patch('vam.llm.openrouter.OpenRouterClient.chat', new_callable=AsyncMock, return_value=('Visible slide.', {})):
        with TestClient(create_app(settings=settings, transcriber=transcriber, backend=p2.TranscriptBackend(), study_model=study_model)) as client:
            vid, _ = p2.ingest(client, audio_media)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = client.get(f'/api/videos/{vid}/study').json()
                if state['artifact']:
                    break
                time.sleep(.02)
            assert state['artifact']
            aid = state['artifact']['artifact_id']
            reg = client.app.state.indexing.registry
            with reg.db.connect(write=True) as conn:
                conn.execute("INSERT INTO study_jobs(job_id,video_id,request_id,options,status,phase,created_at,updated_at) VALUES('pending',?,?,'{}','running','generating',?,?)", (vid, rid(), time.time() + 1, time.time()))
        with TestClient(create_app(settings=settings, backend=p2.TranscriptBackend())) as client:
            state = client.get(f'/api/videos/{vid}/study').json()
            assert state['job']['status'] == 'interrupted'
            assert state['artifact']['artifact_id'] == aid


def test_visual_fallback_for_no_speech_and_missing_model(client, settings, media):
    settings.asr_enabled = False
    vid, _ = p2.ingest(client, media, filename='h264.mp4')
    artifact = generate(client, vid)
    assert {r['source'] for r in artifact['content']['evidence']} == {'event'}
    client.app.state.study.model = None
    response = client.post(f'/api/videos/{vid}/study', json={'request_id': rid()}).json()
    assert study_terminal(client, vid, response['job_id'])['error_code'] == 'model_not_configured'
    assert client.get(f'/api/videos/{vid}/study').json()['artifact']['artifact_id'] == artifact['artifact_id']


def test_batches_validate_coverage_budget_and_timeouts(tmp_path):
    async def scenario():
        cfg = Settings(_env_file=None, data_root=str(tmp_path), study_batch_chars=1000, study_max_batches=1, study_auto_generate=False)
        indexing = IndexingService(cfg)
        service = StudyService(indexing, model=study_model)
        records = [{'evidence_id': 'one', 'text': 'x' * 1001, 'start_t': 1, 'end_t': 2}]
        with pytest.raises(DomainError) as exc:
            service.batches(records)
        assert exc.value.code == 'study_budget_exceeded'
        cfg.study_max_batches = 2
        batches = service.batches(records)
        assert ''.join(b[0]['text'] for b in batches) == records[0]['text']
        async def missing(messages):
            first = json.loads(messages[-1]['content'])[0]['source_id']
            return {'chapters': [{'title': 'one', 'points': ['one'], 'source_ids': [first]}]}
        service.model = missing
        with pytest.raises(ValueError, match='omitted'):
            await service.draft([records[0], dict(records[0], evidence_id='two'), dict(records[0], evidence_id='three')])
        vid = 'v_' + uuid.uuid4().hex
        indexing.registry.directory(vid).mkdir()
        indexing.registry.register(vid, 'sample.mp4', 'source.mp4', 1, 2)
        async def slow(*args):
            await asyncio.sleep(1)
        service._generate = slow
        cfg.study_timeout_s = .01
        job = service.submit(vid, rid())
        await asyncio.gather(*list(service.tasks.values()))
        assert service.job(vid, job['job_id'])['error_code'] == 'study_timeout'
        assert not indexing.leases
    asyncio.run(scenario())


def test_missing_source_is_repaired_without_dropping_material(tmp_path):
    async def scenario():
        calls = []

        async def omit_once(messages):
            records = json.loads(messages[-1]['content'])
            calls.append([r['source_id'] for r in records])
            selected = records[:1] if len(calls) == 1 else records
            return {'chapters': [{'title': '章节', 'points': [r['text']], 'source_ids': [r['source_id']]} for r in selected]}

        cfg = Settings(_env_file=None, data_root=str(tmp_path))
        service = StudyService(IndexingService(cfg), model=omit_once)
        records = [{'evidence_id': key, 'text': key, 'start_t': i, 'end_t': i + 1} for i, key in enumerate(('one', 'two', 'three'))]
        chapters = await service.draft(records)
        assert calls == [['one', 'two', 'three'], ['two', 'three']]
        assert {i for c in chapters for i in c['source_ids']} == {'one', 'two', 'three'}
        assert [(c['start_t'], c['end_t']) for c in chapters] == [(0, 1), (1, 2), (2, 3)]

    asyncio.run(scenario())


def test_search_pagination_ui_limits_and_static(client, audio_media, tmp_path):
    vid, _ = p2.ingest(client, audio_media)
    config = client.get('/api/ui-config').json()
    assert config['max_upload_bytes'] > 0 and 'api_key' not in config
    assert client.get('/api/videos?q=same').json()['total'] == 1
    assert client.get('/api/videos?q=%25').json()['total'] == 0
    assert client.get('/api/videos?offset=1&limit=1').json()['items'] == []
    assert client.get('/api/not-a-real-route').status_code == 404
    assert client.get('/app/deep/link').status_code == 503
    static = tmp_path/'web'
    static.mkdir()
    (static/'index.html').write_text('<html>EviReel App</html>', encoding='utf-8')
    config = Settings(_env_file=None, data_root=str(tmp_path/'static-data'), frontend_dist=str(static))
    with TestClient(create_app(settings=config)) as http:
        assert 'EviReel App' in http.get('/').text
        assert 'EviReel App' in http.get('/app/learning').text
        assert http.get('/api/missing').status_code == 404


def test_schema3_upgrade_preserves_conversations(tmp_path):
    path = tmp_path/'registry.sqlite3'
    migrations = Path(__file__).parents[1]/'vam/storage/migrations'
    with sqlite3.connect(path) as conn:
        for migration in sorted(migrations.glob('00[123]_*.sql')):
            conn.executescript(migration.read_text(encoding='utf-8'))
        conn.execute("INSERT INTO videos(video_id,original_name,source_path,size_bytes,duration,created_at) VALUES('old','old','old',1,1,0)")
        conn.execute("INSERT INTO conversations(conversation_id,video_id,created_at,updated_at) VALUES('old-c','old',0,0)")
        conn.execute('PRAGMA user_version=3')
    db = Database(path)
    with db.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 5  # 005: E2 publication fence
        assert conn.execute('SELECT video_id FROM conversations').fetchone()[0] == 'old'
        assert conn.execute('SELECT trashed_at FROM videos').fetchone()[0] is None


def test_export_continuations_keep_all_text():
    artifact = {'stale': False, 'content': {'title': '课程', 'chapters': [{'title': '长章节', 'start_t': 0, 'end_t': 60,
        'points': ['中' * 1000, '结束内容'], 'source_ids': ['source']} ]}}
    content = export_study(artifact, 'pptx')
    with zipfile.ZipFile(BytesIO(content)) as archive:
        slides = [archive.read(n).decode() for n in archive.namelist() if n.startswith('ppt/slides/slide') and n.endswith('.xml')]
    assert len(slides) > 1 and '结束内容' in ''.join(slides)
    with pytest.raises(DomainError) as exc:
        export_study(artifact, 'pdf', max_pages=1)
    assert exc.value.code == 'export_budget_exceeded'
    assert clock(3601.234, srt=True) == '01:00:01,234'
    assert '结束'.encode() in manuscript([{'start_t': 0, 'end_t': 1, 'text': '结束'}], 'txt')
