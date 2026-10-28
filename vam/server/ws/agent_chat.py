from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid

from fastapi import APIRouter, WebSocket
from pydantic import ValidationError

from vam.protocol import VideoAgentRequest
from vam.video_registry import DomainError
from vam.server.platform_auth import authorize_video

router = APIRouter()


async def _safe_send_text(websocket, payload):
    try:
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
        return True
    except Exception:
        return False


def scope_event(event, video_id, version):
    def bind(value):
        if isinstance(value, list):
            return [bind(item) for item in value]
        if isinstance(value, dict):
            result = {key: bind(item) for key, item in value.items() if key != 'image'}
            if any(key in value for key in ('frame_id', 'doc_id', 'time_range')):
                result['video_id'] = video_id
            return result
        return value
    return bind(event) | {'video_id': video_id, 'index_version': version}


@router.websocket('/ws/agent')
async def agent_chat(websocket: WebSocket):
    await websocket.accept()
    service = websocket.app.state.conversations
    queue = asyncio.Queue(maxsize=64)
    legacy_conversations = {}
    receive_task = event_task = None
    scope = {}
    try:
        await _safe_send_text(websocket, {'type': 'info', 'protocol_version': 2,
                                         'message': 'EviReel ready; persistent conversation_id/request_id supported'})
        receive_task = asyncio.create_task(websocket.receive())
        event_task = asyncio.create_task(queue.get())
        while True:
            done, _ = await asyncio.wait([receive_task, event_task], timeout=20, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                if not await _safe_send_text(websocket, {'type': 'ping', 'protocol_version': 2}):
                    break
                continue
            if event_task in done:
                if not await _safe_send_text(websocket, event_task.result()):
                    break
                event_task = asyncio.create_task(queue.get())
            if receive_task not in done:
                continue
            message = receive_task.result()
            if message['type'] == 'websocket.disconnect':
                break
            receive_task = asyncio.create_task(websocket.receive())
            text = message.get('text')
            if text is None:
                continue
            scope = {}
            try:
                if len(text) > 16000:
                    raise DomainError(413, 'message_too_large', 'Message exceeds limit')
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise ValueError('Expected object')
                if payload.get('type') == 'pong':
                    continue
                for key, prefix in (('video_id', 'v'), ('conversation_id', 'c'), ('request_id', 'r')):
                    if re.fullmatch(prefix + r'_[0-9a-f]{32}', str(payload.get(key, ''))):
                        scope[key] = payload[key]
                if re.fullmatch(r'v_[0-9a-f]{32}', str(payload.get('video_id', ''))):
                    await authorize_video(websocket, payload['video_id'])
                elif getattr(websocket.app.state, 'platform', None) is not None:
                    raise ValueError('video_id required')
                if payload.get('type') == 'reset':
                    video_id = payload.get('video_id')
                    conversation = service.store.create(video_id)
                    legacy_conversations[video_id] = conversation['conversation_id']
                    await _safe_send_text(websocket, {'type': 'conversation_created', 'protocol_version': 2, **conversation})
                    continue
                request = VideoAgentRequest.model_validate(payload)
                if request.conversation_id is None:
                    # Compatibility is private to this socket and video, never a shared default.
                    if request.video_id not in legacy_conversations:
                        legacy_conversations[request.video_id] = service.store.create(request.video_id)['conversation_id']
                    request.conversation_id = legacy_conversations[request.video_id]
                    request.request_id = 'r_' + uuid.uuid4().hex
                service.store.get(request.conversation_id, request.video_id)
                row, fresh = service.submit(request)
                if row['status'] in {'accepted', 'running'}:
                    service.subscribe(request.conversation_id, request.request_id, queue)
                accepted = service.envelope({'type': 'accepted', 'status': row['status'], 'replayed': not fresh},
                                             request.conversation_id, request.request_id)
                if not await _safe_send_text(websocket, accepted):
                    break
                terminal = service.terminal(row)
                if terminal and not await _safe_send_text(websocket, terminal):
                    break
            except DomainError as exc:
                await _safe_send_text(websocket, {'type': 'error', 'protocol_version': 2, **scope, 'code': exc.code, 'message': exc.message})
            except (ValueError, ValidationError):
                await _safe_send_text(websocket, {'type': 'error', 'protocol_version': 2, **scope,
                                                 'code': 'invalid_request', 'message': 'Invalid video, conversation, request or tool arguments'})
    finally:
        service.unsubscribe(queue)
        for task in (receive_task, event_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        with contextlib.suppress(Exception):
            await websocket.close()
