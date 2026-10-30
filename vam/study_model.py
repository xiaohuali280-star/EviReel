"""Bounded OpenAI-compatible study requests; never publish partial responses."""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

from vam.video_registry import DomainError

logger = logging.getLogger(__name__)
MAX_OUTPUT_CHARS = 100000
MAX_EVENT_CHARS = 400000


def _error(code):
    # Provider bodies may echo credentials or private input. Expose fixed codes only.
    return DomainError(502, code, 'Study model request could not be completed')


def _status_error(status):
    if status in (401, 403):
        return _error('study_model_auth_failed')
    if status == 429:
        return _error('study_model_rate_limited')
    if status == 404 or status >= 500:
        return _error('study_model_unavailable')
    return _error('study_model_request_rejected')


async def _events(response):
    data, size = [], 0
    async for line in response.aiter_lines():
        if len(line) > MAX_EVENT_CHARS:
            raise _error('study_invalid_output')
        if not line:
            if data:
                yield '\n'.join(data)
            data, size = [], 0
        elif line.startswith('data:'):
            value = line[5:].removeprefix(' ')
            size += len(value)
            if size > MAX_EVENT_CHARS:
                raise _error('study_invalid_output')
            data.append(value)
    if data:
        yield '\n'.join(data)


async def _stream_content(response):
    parts, size, finish, done = [], 0, None, False
    try:
        async for data in _events(response):
            if data.strip() == '[DONE]':
                done = True
                break
            chunk = json.loads(data)
            if chunk.get('error'):
                raise _error('study_model_request_rejected')
            for choice in chunk.get('choices', []):
                if choice.get('index', 0) != 0:
                    continue
                value = choice.get('delta', {}).get('content')
                if value is not None:
                    if not isinstance(value, str):
                        raise _error('study_invalid_output')
                    size += len(value)
                    if size > MAX_OUTPUT_CHARS:
                        raise _error('study_invalid_output')
                    parts.append(value)
                finish = choice.get('finish_reason') or finish
                if finish and finish != 'stop':
                    raise _error('study_model_incomplete')
    except (ValueError, KeyError, TypeError, AttributeError):
        raise _error('study_invalid_output') from None
    if not done or finish != 'stop':
        raise _error('study_model_incomplete')
    if not parts:
        raise _error('study_invalid_output')
    return ''.join(parts)


async def _json_content(response):
    chunks, size = [], 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_EVENT_CHARS:
            raise _error('study_invalid_output')
        chunks.append(chunk)
    try:
        choice = json.loads(b''.join(chunks))['choices'][0]
        if choice.get('finish_reason') != 'stop':
            raise _error('study_model_incomplete')
        raw = choice['message']['content']
        if not isinstance(raw, str) or not raw or len(raw) > MAX_OUTPUT_CHARS:
            raise _error('study_invalid_output')
        return raw
    except (ValueError, KeyError, IndexError, TypeError):
        raise _error('study_invalid_output') from None


async def request_study(settings, messages):
    if not settings.api_key:
        raise DomainError(503, 'model_not_configured', 'Configure a model provider to generate notes')

    from vam.gateway import ProviderError, estimate_tokens, get_gateway

    async def attempt():
        async with httpx.AsyncClient(timeout=settings.study_model_timeout_s) as client:
            async with client.stream('POST', settings.chat_base_url.rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer ' + settings.api_key},
                json={'model': settings.model_id, 'messages': messages, 'temperature': 0,
                      'max_tokens': 6000, 'stream': settings.study_stream}) as response:
                if not response.is_success:
                    # Retry only transient statuses, without logging response bodies.
                    status = response.status_code
                    raise ProviderError(_status_error(status).code, status=status,
                                        retryable=status in (429, 500, 502, 503, 504), sent=status >= 500)
                content_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                raw = await (_stream_content(response) if content_type == 'text/event-stream' else _json_content(response))
                return raw, None     # streamed responses carry no usage: the ledger keeps the estimate

    chars = sum(len(m.get('content') or '') for m in messages if isinstance(m.get('content'), str))
    try:
        # E3: admission, budget reservation, per-attempt timeout and retries happen in the ModelGateway.
        return await get_gateway().run(operation='study', model=settings.model_id, attempt_fn=attempt,
                                       est_input_tokens=estimate_tokens(chars), est_output_tokens=6000,
                                       timeout=settings.study_model_timeout_s, attempts=settings.study_model_attempts)
    except ProviderError as exc:
        if exc.status is not None:
            raise _status_error(exc.status) from None
        if exc.code == 'provider_timeout':
            raise _error('study_timeout') from None
        raise _error('study_model_connection_failed') from None
