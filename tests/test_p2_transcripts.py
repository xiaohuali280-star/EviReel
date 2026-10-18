"""P2 contracts: real audio/SQLite/HTTP, explicit deterministic ASR/embedding mocks."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import wave

from fastapi.testclient import TestClient
import pytest

import test_p1_video_management as p1
from test_p1_video_management import ColorBackend, terminal
from vam.asr.audio import ASRError, extract_audio
from vam.asr.chunker import make_chunks, normalize_segments
from vam.asr.transcriber import FasterWhisperTranscriber, MockTranscriber
from vam.asr.worker import run as run_worker
from vam.retrieval.frame_store import FrameStore
from vam.server.app import create_app
from vam.storage.database import Database

media = p1.media
settings = p1.settings


class TranscriptBackend(ColorBackend):
    fail_document = False
    fail_query = False
    bad_vectors = None
    instructions = None

    def embed_texts_batch(self, texts, *, instruction, batch_size):
        if self.instructions is None:
            self.instructions = []
        self.instructions.append(instruction)
        if self.fail_document and instruction == 'search_document':
            raise RuntimeError('mock document outage')
        if self.bad_vectors is not None and instruction == 'search_document':
            return self.bad_vectors, 3
        return super().embed_texts_batch(texts, instruction=instruction, batch_size=batch_size)

    def embed_text(self, text, *, instruction):
        if self.fail_query:
            raise RuntimeError('mock query outage')
        return super().embed_text(text, instruction=instruction)


class ControlledTranscriber(MockTranscriber):
    fail = False

    def transcribe(self, audio_path):
        if self.fail:
            self.calls += 1
            raise ASRError('asr_failed', 'Mock ASR failure')
        return super().transcribe(audio_path)


@pytest.fixture(scope='session')
def audio_media(media):
    for color in ('red', 'blue'):
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        f'color=c={color}:s=160x120:r=4', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                        '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-y',
                        str(media / f'{color}-audio.mp4')], check=True, timeout=30)
    return media


@pytest.fixture
def transcriber():
    return ControlledTranscriber([
        {'start': .1, 'end': .65, 'text': '红色 red slide。', 'avg_logprob': -.12, 'no_speech_prob': .01},
        {'start': 1.05, 'end': 1.8, 'text': '蓝色 blue vector.'},
    ], language='zh')


@contextmanager
def p2_client(settings, transcriber, backend=None, indexer=None):
    backend = backend or TranscriptBackend()
    with patch('vam.llm.openrouter.OpenRouterClient.chat', new_callable=AsyncMock, return_value=('Visible slide.', {})):
        with TestClient(create_app(settings=settings, backend=backend, transcriber=transcriber, indexer=indexer)) as client:
            yield client, backend


def ingest(client, media, filename='red-audio.mp4'):
    response = client.post('/api/videos', files={'video': ('same.mp4', (media / filename).read_bytes(), 'video/mp4')})
    assert response.status_code == 202, response.text
    data = response.json()
    return data['video_id'], terminal(client, data['job_id'])


def retry(client, video_id):
    response = client.post(f'/api/videos/{video_id}/transcripts/index')
    assert response.status_code == 202, response.text
    return terminal(client, response.json()['job_id'])


def test_full_ingest_and_semantic_query(settings, audio_media, transcriber):
    with p2_client(settings, transcriber) as (client, backend):
        vid, job = ingest(client, audio_media)
        assert job['status'] == 'succeeded', job
        assert job['result']['stages']['transcript']['status'] == 'ready'
        detail = client.get(f'/api/videos/{vid}').json()
        assert detail['capabilities'] == {'vision': True, 'playback': True, 'transcript': True, 'transcript_search': True}
        chunks = client.get(f'/api/videos/{vid}/transcripts').json()
        assert chunks['total'] == 2
        assert chunks['items'][0]['segment_ids'] == ['s-000000']
        assert chunks['items'][0]['timing_precision'] == 'segment'
        raw = client.get(f'/api/videos/{vid}/transcripts?kind=segments&offset=0&limit=1').json()
        assert raw['total'] == 2 and len(raw['items']) == 1
        assert raw['items'][0]['quality']['avg_logprob'] == -.12
        assert 'embedding' not in json.dumps(chunks)
        result = client.post(f'/api/videos/{vid}/transcripts/search', json={'query': 'blue', 'top_k': 1}).json()
        assert result['results'][0]['start_t'] == 1.05
        assert result['results'][0]['end_t'] == 1.8
        assert result['results'][0]['video_id'] == vid
        assert 'search_document' in backend.instructions and 'search_query' in backend.instructions
        reg = client.app.state.indexing.registry
        row = reg.get(vid)
        assert row['transcript_generation'] == row['active_generation']
        path = reg.asset(vid, f"memory/{row['active_generation']}.sqlite3")
        with sqlite3.connect(path) as conn:
            assert conn.execute('SELECT COUNT(*) FROM frames').fetchone()[0] > 0
            assert conn.execute('SELECT dim,embedding_fingerprint,source_sha256 FROM transcript_versions').fetchone()[0] == 3
        assert not list(reg.asset(vid, 'cache/transcript-work').glob('asr-*'))


def test_idempotent_jobs_and_visual_rebuild_keep_transcripts(settings, audio_media, transcriber):
    with p2_client(settings, transcriber) as (client, _):
        vid, _ = ingest(client, audio_media)
        initial = client.get(f'/api/videos/{vid}/transcripts').json()
        for _ in range(2):
            job = retry(client, vid)
            assert job['kind'] == 'transcript' and job['result']['stages']['transcript']['reused'] is True
        jid = client.post(f'/api/videos/{vid}/index').json()['job_id']
        assert terminal(client, jid)['status'] == 'succeeded'
        assert transcriber.calls == 1
        assert client.get(f'/api/videos/{vid}/transcripts').json()['items'] == initial['items']
        service = client.app.state.indexing
        row = service.registry.get(vid)
        assert row['active_generation'] != row['transcript_generation']
        store = service._transcript_store(vid)
        with store.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM transcript_versions').fetchone()[0] == 1
            assert conn.execute('SELECT COUNT(*) FROM transcript_chunks').fetchone()[0] == 2
        # A real visual snapshot persist in the original shared DB cannot erase text.
        original = FrameStore(backend=service.backend, persist_path=str(store.path))
        asyncio.run(original._persist())
        assert original.transcripts(vid).list()['total'] == 2


def test_transcript_video_time_isolation_and_delete(settings, audio_media, transcriber):
    with p2_client(settings, transcriber) as (client, _):
        a, _ = ingest(client, audio_media)
        transcriber.segments = [{'start': .2, 'end': .8, 'text': 'Different blue lesson.'}]
        b, _ = ingest(client, audio_media, 'blue-audio.mp4')
        for vid in (a, b):
            result = client.post(f'/api/videos/{vid}/transcripts/search', json={'query': 'blue', 'min_time': 1, 'max_time': 1.9}).json()
            assert all(h['video_id'] == vid and h['end_t'] >= 1 for h in result['results'])
        assert client.post(f'/api/videos/{b}/transcripts/search', json={'query': 'blue', 'min_time': 1}).json()['results'] == []
        assert client.get(f'/api/videos/{a}/transcripts?min_time=1&max_time=1.9').json()['total'] == 1
        assert client.delete(f'/api/videos/{a}').status_code == 204
        assert client.get(f'/api/videos/{b}/transcripts').json()['total'] == 1


def test_asr_failure_keeps_old_text_and_search(settings, audio_media, transcriber):
    with p2_client(settings, transcriber) as (client, _):
        vid, _ = ingest(client, audio_media)
        initial = client.get(f'/api/videos/{vid}/transcripts').json()
        transcriber.identity, transcriber.fail = 'new-broken-model', True
        job = retry(client, vid)
        assert job['status'] == 'failed' and job['error_code'] == 'asr_failed'
        assert client.get(f'/api/videos/{vid}/transcripts').json() == initial
        assert client.post(f'/api/videos/{vid}/transcripts/search', json={'query': 'blue'}).status_code == 200
        transcriber.fail = False
        assert retry(client, vid)['status'] == 'succeeded'


def test_embedding_failure_preserves_readable_text_and_prior_search(settings, audio_media, transcriber):
    with p2_client(settings, transcriber) as (client, backend):
        vid, _ = ingest(client, audio_media)
        first = client.get(f'/api/videos/{vid}/transcripts').json()
        transcriber.segments = [{'start': .3, 'end': 1., 'text': 'A newly transcribed blue lesson.'}]
        backend.fail_document = True
        job = retry(client, vid)
        assert job['status'] == 'partial'
        current = client.get(f'/api/videos/{vid}/transcripts').json()
        assert current['text_version'] != first['text_version']
        assert current['search_version'] == first['search_version'] and current['search_uses_older_text']
        assert 'newly' in current['items'][0]['text']
        assert client.post(f'/api/videos/{vid}/transcripts/search', json={'query': 'red'}).status_code == 200
        calls = transcriber.calls
        backend.fail_document = False
        assert retry(client, vid)['status'] == 'succeeded'
        assert transcriber.calls == calls
        assert not client.get(f'/api/videos/{vid}/transcripts').json()['search_uses_older_text']


@pytest.mark.parametrize('vectors', [[], [[1., 0.]], [[float('nan'), 0., 0.]], [[0., 0., 0.]], [[True, 0., 0.]]])
def test_bad_embeddings_leave_text_readable(settings, audio_media, transcriber, vectors):
    transcriber.segments = [{'start': .1, 'end': .9, 'text': 'A red sentence.'}]
    backend = TranscriptBackend()
    backend.bad_vectors = vectors
    with p2_client(settings, transcriber, backend) as (client, _):
        vid, job = ingest(client, audio_media)
        assert job['status'] == 'partial'
        detail = client.get(f'/api/videos/{vid}').json()
        assert detail['capabilities']['vision'] and detail['capabilities']['transcript']
        assert not detail['capabilities']['transcript_search']
        assert client.post(f'/api/videos/{vid}/transcripts/search', json={'query': 'red'}).status_code == 409
        backend.bad_vectors = None
        assert retry(client, vid)['status'] == 'succeeded'
        assert transcriber.calls == 1


def test_asr_and_vision_independent_outcomes(settings, audio_media, transcriber):
    backend = TranscriptBackend()
    backend.fail = True
    with p2_client(settings, transcriber, backend) as (client, _):
        vid, job = ingest(client, audio_media)
        assert job['status'] == 'partial'
        assert job['result']['stages']['vision']['status'] == 'failed'
        assert job['result']['stages']['transcript']['status'] == 'ready'
        assert client.get(f'/api/videos/{vid}/frames').status_code == 409
        assert client.get(f'/api/videos/{vid}/transcripts').json()['total'] == 2
        assert retry(client, vid)['status'] == 'succeeded'


def test_vision_ready_asr_failed_is_partial(settings, audio_media, transcriber):
    transcriber.fail = True
    with p2_client(settings, transcriber) as (client, _):
        vid, job = ingest(client, audio_media)
        assert job['status'] == 'partial'
        assert job['result']['stages']['vision']['status'] == 'ready'
        assert job['result']['stages']['transcript']['error_code'] == 'asr_failed'
        assert client.get(f'/api/videos/{vid}/frames').json()['total'] > 0
        assert client.get(f'/api/videos/{vid}/transcripts').status_code == 409


@pytest.mark.parametrize('mode', ['no_audio', 'empty', 'disabled'])
def test_empty_disabled_and_no_audio_are_distinct(settings, audio_media, transcriber, mode):
    if mode == 'disabled':
        settings.asr_enabled = False
    if mode == 'empty':
        transcriber.segments = [{'start': .1, 'end': 1., 'text': '  '}]
    with p2_client(settings, transcriber) as (client, _):
        vid, job = ingest(client, audio_media, 'red.mp4' if mode == 'no_audio' else 'red-audio.mp4')
        assert job['status'] == 'succeeded'
        stage = job['result']['stages']['transcript']
        assert stage['status'] == ('empty' if mode == 'empty' else 'skipped')
        assert stage['reason'] == ('empty_transcription' if mode == 'empty' else mode)
        assert transcriber.calls == (1 if mode == 'empty' else 0)
        if mode == 'empty':
            assert client.get(f'/api/videos/{vid}/transcripts').json()['items'] == []


def test_restart_and_registry_v1_migration(settings, audio_media, transcriber, tmp_path):
    with p2_client(settings, transcriber) as (client, _):
        vid, job = ingest(client, audio_media)
        expected = client.get(f'/api/videos/{vid}/transcripts').json()
    with p2_client(settings, transcriber) as (client, _):
        assert client.get(f'/api/jobs/{job["job_id"]}').json()['status'] == 'succeeded'
        assert client.get(f'/api/videos/{vid}/transcripts').json() == expected
    legacy = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(legacy) as conn:
        conn.executescript((Path(__file__).parents[1] / 'vam/storage/migrations/001_registry.sql').read_text())
        conn.execute("INSERT INTO videos(video_id,original_name,source_path,size_bytes,duration,created_at) VALUES('old','old.mp4','source.mp4',12,2,0)")
        conn.execute('PRAGMA user_version=1')
    database = Database(legacy)
    with database.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 3
        assert conn.execute('SELECT original_name,transcript_status FROM videos').fetchone()[1] == 'not_started'


@pytest.mark.parametrize('query', [{'query': ' '}, {'query': 'x', 'min_time': -1}, {'query': 'x', 'min_time': 2, 'max_time': 1}, {'query': 'x', 'max_time': 'NaN'}, {'query': 'x', 'store_path': '/tmp/other'}])
def test_search_validation(settings, audio_media, transcriber, query):
    with p2_client(settings, transcriber) as (client, _):
        vid, _ = ingest(client, audio_media)
        assert client.post(f'/api/videos/{vid}/transcripts/search', json=query).status_code == 422


def test_missing_resource_model_change_and_query_failure(settings, audio_media, transcriber):
    with p2_client(settings, transcriber) as (client, backend):
        assert client.get('/api/videos/not-a-video/transcripts').status_code == 404
        assert client.post('/api/videos/not-a-video/transcripts/index').status_code == 404
        vid, _ = ingest(client, audio_media)
        assert client.get(f'/api/videos/{vid}/transcripts?kind=unknown').status_code == 422
        assert client.get(f'/api/videos/{vid}/transcripts?min_time=2&max_time=1').status_code == 422
        backend.fail_query = True
        assert client.post(f'/api/videos/{vid}/transcripts/search', json={'query': 'red'}).status_code == 502
        backend.fail_query = False
        backend.name = 'different-same-dimension-model'
        assert client.post(f'/api/videos/{vid}/transcripts/search', json={'query': 'red'}).status_code == 409
        calls = transcriber.calls
        assert retry(client, vid)['status'] == 'succeeded'
        assert transcriber.calls == calls


@pytest.mark.parametrize('start,end', [(-1, 2), (2, 1), (0, 0), (True, 1), (float('nan'), 1), (0, float('inf')), ('bad', 1)])
def test_invalid_segment_times_rejected(start, end):
    with pytest.raises(ASRError, match='invalid segment'):
        normalize_segments([{'start': start, 'end': end, 'text': 'x'}], offset_s=0, duration_s=3)


def test_normalization_overlap_offset_languages_and_chunk_bounds():
    raw = [{'start': 1, 'end': 3, 'text': ' Hello world! ', 'language': 'en'},
           {'start': 0, 'end': 1.5, 'text': '你好。' + '长句' * 50, 'language': 'zh'},
           {'start': 7, 'end': 8, 'text': 'outside'}, {'start': 0, 'end': .1, 'text': ' '}]
    segments = normalize_segments(raw, offset_s=-.25, duration_s=2.5)
    assert [s.start_t for s in segments] == [0, .75]
    assert [s.end_t for s in segments] == [1.25, 2.5]
    assert segments[0].raw_start_t == 0 and segments[0].time_clipped
    assert segments[1].raw_end_t == 3 and segments[1].time_clipped
    chunks = make_chunks(segments, max_chars=16, max_duration_s=.5, pause_s=.2)
    assert all(len(c.text) <= 16 and c.segment_ids and c.timing_precision == 'segment' for c in chunks)
    assert any(c.end_t-c.start_t > .5 for c in chunks)  # explicit segment-time precision
    assert {s.language for s in segments} == {'zh', 'en'}


def test_transcript_output_limits_and_pause_chunking():
    raw = [{'start': 0, 'end': 1, 'text': 'unpunctuated'}, {'start': 4, 'end': 5, 'text': '中文 English'}]
    with pytest.raises(ASRError) as exc:
        normalize_segments(raw, offset_s=0, duration_s=6, max_segments=1)
    assert exc.value.code == 'transcript_limit'
    with pytest.raises(ASRError):
        normalize_segments(raw, offset_s=0, duration_s=6, max_chars=2)
    segments = normalize_segments(raw, offset_s=0, duration_s=6)
    assert len(make_chunks(segments, max_chars=100, max_duration_s=10, pause_s=.5)) == 2


def test_real_audio_format_and_timeout_cleanup(settings, audio_media, tmp_path):
    path = tmp_path / 'audio.wav'
    asset = extract_audio(audio_media / 'red-audio.mp4', path, settings)
    assert asset.offset_s == 0 and 1.8 <= asset.duration_s <= 2.2
    with wave.open(str(path)) as handle:
        assert (handle.getnchannels(), handle.getframerate(), handle.getsampwidth()) == (1, 16000, 2)
    assert extract_audio(audio_media / 'red.mp4', tmp_path / 'none.wav', settings) is None
    with patch('vam.asr.audio.subprocess.run', side_effect=subprocess.TimeoutExpired('ffmpeg', .01)):
        with pytest.raises(ASRError) as exc:
            extract_audio(audio_media / 'red-audio.mp4', path, settings)
        assert exc.value.code == 'audio_timeout'


def test_audio_offset_mapping(settings, tmp_path):
    meta = {'duration': 4., 'streams': [{'codec_type': 'video', 'start_time': '10'}, {'codec_type': 'audio', 'start_time': '10.75'}]}
    output = tmp_path / 'audio.wav'
    def wav(*args, **kwargs):
        with wave.open(str(output), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b'\x00\x00' * 16000)
    with patch('vam.asr.audio._probe_video_metadata', return_value=meta), patch('vam.asr.audio.subprocess.run', side_effect=wav):
        assert extract_audio(tmp_path / 'source.mp4', output, settings).offset_s == .75


def test_faster_whisper_worker_contract_no_network(settings, tmp_path):
    captured = {}
    class Model:
        def __init__(self, model, **kwargs):
            captured.update(model=model, **kwargs)
        def transcribe(self, path, **kwargs):
            captured['transcribe'] = kwargs
            segment = SimpleNamespace(start=.1, end=1., text='Known line.', avg_logprob=-.2, no_speech_prob=.01, compression_ratio=1., temperature=0.)
            return iter([segment]), SimpleNamespace(language='en')
    cfg = FasterWhisperTranscriber(settings).config()
    audio = tmp_path / 'worker.wav'
    with wave.open(str(audio), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\x00\x00' * 16000)
    with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=Model)}), patch('vam.asr.worker.resolve_model', return_value='complete-model'):
        result = run_worker({'audio_path': str(audio), 'config': cfg})
    assert result['segments'][0]['text'] == 'Known line.'
    assert captured['local_files_only'] is True and captured['device'] == 'cpu'
    assert captured['transcribe']['word_timestamps'] is False
    transcriber = FasterWhisperTranscriber(settings)
    with patch('vam.asr.transcriber.subprocess.run', side_effect=subprocess.TimeoutExpired('asr', .01)):
        with pytest.raises(ASRError) as exc:
            transcriber.transcribe(tmp_path / 'test.wav')
        assert exc.value.code == 'asr_timeout'
    with patch('vam.asr.transcriber.subprocess.run', return_value=SimpleNamespace(stdout='not json', returncode=1)):
        with pytest.raises(ASRError) as exc:
            transcriber.transcribe(tmp_path / 'test.wav')
        assert exc.value.code == 'asr_worker_failed'


def test_optional_dependency_and_model_errors_are_explicit(settings):
    with patch.dict(sys.modules, {'faster_whisper': None}):
        result = run_worker({'config': {}})
    assert result['error_code'] == 'asr_dependency_missing'
    class BrokenModel:
        def __init__(self, *args, **kwargs):
            raise RuntimeError('no cached model')
    with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=BrokenModel)}), patch('vam.asr.worker.resolve_model', return_value='complete-model'):
        result = run_worker({'config': FasterWhisperTranscriber(settings).config()})
    assert result['error_code'] == 'asr_model_unavailable'


def test_transcript_shutdown_restart_preserves_old_version(settings, audio_media, transcriber):
    with p2_client(settings, transcriber) as (client, _):
        vid, _ = ingest(client, audio_media)
        expected = client.get(f'/api/videos/{vid}/transcripts').json()
        service = client.app.state.indexing
        jid = service.registry.create_job(vid, 20, 'transcript')
        service.registry.update_video(vid, transcript_status='running')
    with p2_client(settings, transcriber) as (client, _):
        assert client.get(f'/api/jobs/{jid}').json()['status'] == 'interrupted'
        assert client.get(f'/api/videos/{vid}').json()['transcript']['status'] == 'interrupted'
        assert client.get(f'/api/videos/{vid}/transcripts').json() == expected
        assert retry(client, vid)['status'] == 'succeeded'


def test_transcript_job_blocks_delete_and_duplicate_index(settings, audio_media, transcriber):
    entered, release = threading.Event(), threading.Event()
    original = transcriber.transcribe
    def blocked(path):
        entered.set()
        if not release.wait(10):
            raise ASRError('asr_timeout', 'Mock timed out')
        return original(path)
    with p2_client(settings, transcriber) as (client, _):
        vid, _ = ingest(client, audio_media)
        expected = client.get(f'/api/videos/{vid}/transcripts').json()
        transcriber.identity = 'blocking-new-model'
        transcriber.transcribe = blocked
        jid = client.post(f'/api/videos/{vid}/transcripts/index').json()['job_id']
        try:
            assert entered.wait(5)
            assert client.delete(f'/api/videos/{vid}').status_code == 409
            assert client.post(f'/api/videos/{vid}/transcripts/index').status_code == 409
            assert client.post(f'/api/videos/{vid}/index').status_code == 409
            assert client.get(f'/api/videos/{vid}/transcripts').json() == expected
        finally:
            release.set()
        assert terminal(client, jid)['status'] == 'succeeded'


def test_raw_text_and_decimal_sentence_preserved():
    segments = normalize_segments([{'start': 0, 'end': 1, 'text': '  Pi is 3.14.  Next sentence!  '}], offset_s=0, duration_s=2)
    assert segments[0].raw_text == '  Pi is 3.14.  Next sentence!  '
    chunks = make_chunks(segments)
    assert chunks[0].text == 'Pi is 3.14.'
    assert chunks[1].text == 'Next sentence!'


def test_real_delayed_audio_keeps_video_timeline(settings, tmp_path):
    source = tmp_path / 'delayed.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=160x120:r=4',
                    '-itsoffset', '0.5', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '2',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-y', str(source)], check=True, timeout=30)
    asset = extract_audio(source, tmp_path / 'mapped.wav', settings)
    assert .45 <= asset.offset_s <= .55
    normalized = normalize_segments([{'start': 0, 'end': .5, 'text': 'delayed'}], offset_s=asset.offset_s, duration_s=2)
    assert .45 <= normalized[0].start_t <= .55
