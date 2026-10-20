"""Exercise the real study HTTP boundary, including fragmented SSE and failures."""
import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from vam.config import Settings
from vam.indexing.service import IndexingService
from vam.study import StudyService
from vam.video_registry import DomainError


RAW = json.dumps({'chapters': [{'title': '集合', 'points': ['交集是共同元素。'], 'source_ids': ['one']}]}, ensure_ascii=False)
RECORDS = [{'evidence_id': 'one', 'text': '交集是共同元素。', 'start_t': 1, 'end_t': 2}]


class FragmentedStream(httpx.AsyncByteStream):
    def __init__(self, body, *, fail=False):
        self.body, self.fail = body, fail
        self.closed = False

    async def __aiter__(self):
        # Splits UTF-8 characters and SSE delimiters across network reads.
        for offset in range(0, len(self.body), 7):
            yield self.body[offset:offset + 7]
        if self.fail:
            raise httpx.RemoteProtocolError('connection closed during generation')

    async def aclose(self):
        self.closed = True


def sse(raw=RAW, finish='stop', done=True):
    events = [': keepalive\n\n', 'data: {"choices":[]}\n\n']
    for part in (raw[:31], raw[31:]):
        events.append('data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': part}, 'finish_reason': None}]}, ensure_ascii=False) + '\n\n')
    if finish:
        events.append('data: ' + json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}) + '\n\n')
    if done:
        events.append('data: [DONE]\n\n')
    return ''.join(events).encode()


def service(tmp_path, **options):
    cfg = Settings(_env_file=None, data_root=str(tmp_path), dashscope_api_key='', openrouter_api_key='',
                   siliconflow_api_key='test-only-not-a-secret', **options)
    return StudyService(IndexingService(cfg))


def client_factory(handler):
    real_client = httpx.AsyncClient
    return lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)


def test_study_uses_stream_and_validates_fragmented_unicode(tmp_path):
    streams = []

    def handler(request):
        if not json.loads(request.content).get('stream'):
            raise httpx.RemoteProtocolError('buffered response closed before completion')
        stream = FragmentedStream(sse())
        streams.append(stream)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

    with patch('httpx.AsyncClient', client_factory(handler)):
        chapters = asyncio.run(service(tmp_path).draft(RECORDS))
    assert chapters == [{'title': '集合', 'points': ['交集是共同元素。'], 'source_ids': ['one'], 'start_t': 1, 'end_t': 2}]
    assert streams and all(stream.closed for stream in streams)


@pytest.mark.parametrize('finish,done', [('length', True), ('content_filter', True), (None, False)])
def test_never_accepts_truncated_or_filtered_response(tmp_path, finish, done):
    def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=FragmentedStream(sse(finish=finish, done=done)))
    with patch('httpx.AsyncClient', client_factory(handler)):
        with pytest.raises(DomainError) as exc:
            asyncio.run(service(tmp_path).draft(RECORDS))
    assert exc.value.code == 'study_model_incomplete'


def test_retry_discards_partial_output_and_closes_streams(tmp_path):
    streams = []

    def handler(request):
        first = not streams
        stream = FragmentedStream(sse(raw='partial', finish=None, done=False) if first else sse(), fail=first)
        streams.append(stream)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

    with patch('httpx.AsyncClient', client_factory(handler)):
        chapters = asyncio.run(service(tmp_path).draft(RECORDS))
    assert len(chapters) == 1 and len(streams) == 2
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize('status,code,calls', [(401, 'study_model_auth_failed', 1), (403, 'study_model_auth_failed', 1),
    (404, 'study_model_unavailable', 1), (400, 'study_model_request_rejected', 1),
    (429, 'study_model_rate_limited', 2), (503, 'study_model_unavailable', 2)])
def test_provider_errors_are_actionable_and_redacted(tmp_path, status, code, calls):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={'error': {'message': 'PRIVATE provider text test-only-not-a-secret'}})

    with patch('httpx.AsyncClient', client_factory(handler)):
        with pytest.raises(DomainError) as exc:
            asyncio.run(service(tmp_path).draft(RECORDS))
    assert exc.value.code == code and len(seen) == calls
    assert 'PRIVATE' not in str(exc.value) and 'test-only' not in str(exc.value)


def test_network_failures_have_bounded_retries(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        raise httpx.RemoteProtocolError('PRIVATE connection text')

    with patch('httpx.AsyncClient', client_factory(handler)):
        with pytest.raises(DomainError) as exc:
            asyncio.run(service(tmp_path).draft(RECORDS))
    assert exc.value.code == 'study_model_connection_failed' and len(seen) == 2
    assert 'PRIVATE' not in str(exc.value)


def test_compatibility_nonstream_setting(tmp_path):
    def handler(request):
        assert json.loads(request.content)['stream'] is False
        return httpx.Response(200, json={'choices': [{'message': {'content': RAW}, 'finish_reason': 'stop'}]})

    with patch('httpx.AsyncClient', client_factory(handler)):
        assert len(asyncio.run(service(tmp_path, study_stream=False).draft(RECORDS))) == 1


def test_stream_output_is_bounded(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=FragmentedStream(sse(raw='x' * 100001)))

    with patch('httpx.AsyncClient', client_factory(handler)):
        with pytest.raises(DomainError) as exc:
            asyncio.run(service(tmp_path).draft(RECORDS))
    assert exc.value.code == 'study_invalid_output'


def test_attempt_deadline_closes_stream(tmp_path):
    class SlowStream(FragmentedStream):
        async def __aiter__(self):
            await asyncio.sleep(10)
            yield self.body

    stream = SlowStream(sse())

    def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

    with patch('httpx.AsyncClient', client_factory(handler)):
        with pytest.raises(DomainError) as exc:
            asyncio.run(service(tmp_path, study_model_timeout_s=.01, study_model_attempts=1).draft(RECORDS))
    assert exc.value.code == 'study_timeout' and stream.closed


def test_cancellation_is_not_retried_and_closes_stream(tmp_path):
    async def scenario():
        entered = asyncio.Event()

        class WaitingStream(FragmentedStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.sleep(10)
                yield self.body

        stream = WaitingStream(sse())
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

        with patch('httpx.AsyncClient', client_factory(handler)):
            task = asyncio.create_task(service(tmp_path).draft(RECORDS))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(calls) == 1 and stream.closed

    asyncio.run(scenario())
