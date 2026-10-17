from __future__ import annotations
import asyncio
import contextlib
import json
import logging
from fastapi import APIRouter, WebSocket
from pydantic import ValidationError

from vam.protocol import RetrieveToolArgs, SummarizeToolArgs, VideoAgentRequest
from vam.agent import stream_agent_response, stream_retrieve_tool_request, stream_summarize_tool_request
from vam.video_registry import DomainError

router = APIRouter()
logger = logging.getLogger(__name__)

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
            result = {key: bind(item) for key, item in value.items()}
            if any(key in value for key in ("frame_id", "doc_id", "time_range")):
                result["video_id"] = video_id
            return result
        return value
    return bind(event) | {"video_id": video_id, "index_version": version}

@router.websocket("/ws/agent")
async def agent_chat(websocket: WebSocket):
    await websocket.accept()
    history = []
    bound_video = None
    send_lock = asyncio.Lock()

    async def send(event):
        async with send_lock:
            return await _safe_send_text(websocket, event)

    async def keep_alive():
        while True:
            await asyncio.sleep(20)
            if not await send({"type": "ping"}):
                return

    ping = asyncio.create_task(keep_alive())
    try:
        await send({"type": "info", "message": "EviReel endpoint ready; video_id is required"})
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                continue
            if len(text) > 16_000:
                await send({"type": "error", "code": "message_too_large", "message": "Message exceeds limit"})
                continue
            try:
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise ValueError("Expected a JSON object")
                if payload.get("type") == "pong":
                    continue
                if payload.get("type") == "reset":
                    history.clear()
                    bound_video = None
                    await send({"type": "info", "message": "chat reset"})
                    continue
                request = VideoAgentRequest.model_validate(payload)
                service = websocket.app.state.indexing
                async with service.memory_context(request.video_id) as (row, store):
                    if bound_video != request.video_id:
                        history.clear()
                        bound_video = request.video_id
                    if request.type == "text":
                        events = stream_agent_response(transcript=request.text, history=history, store_instance=store)
                    elif request.name == "retrieve":
                        args = RetrieveToolArgs.model_validate(request.args)
                        events = stream_retrieve_tool_request(transcript=args.question, history=history, store_instance=store)
                    else:
                        args = SummarizeToolArgs.model_validate(request.args)
                        events = stream_summarize_tool_request(args=args, history=history, store_instance=store)
                    async for event in events:
                        if not await send(scope_event(event, request.video_id, row["active_generation"])):
                            return
            except DomainError as exc:
                await send({"type": "error", "code": exc.code, "message": exc.message})
            except (ValueError, ValidationError):
                await send({"type": "error", "code": "invalid_request", "message": "Invalid request; include a valid video_id and text/tool arguments"})
            except Exception as exc:
                logger.warning("agent_failed video_id=%s error_type=%s", bound_video, type(exc).__name__)
                await send({"type": "error", "code": "agent_failed", "message": "Agent/model request failed; retry is available"})
    finally:
        ping.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ping
        with contextlib.suppress(Exception):
            await websocket.close()
