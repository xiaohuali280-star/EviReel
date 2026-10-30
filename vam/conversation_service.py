"""Bounded background executions; sockets subscribe without owning model lifetime."""
from __future__ import annotations

import asyncio
import json
import logging
import re

from vam.agent import stream_agent_response, stream_retrieve_tool_request, stream_summarize_tool_request
from vam.memory.context_manager import ContextManager, ConversationContext
from vam.memory.conversation import ConversationStore
from vam.protocol import RetrieveToolArgs, SummarizeToolArgs
from vam.video_registry import DomainError
from vam.chat_actions import ChatActions
from vam.chat_text import answer_text
from vam.events import LocalEventBus

logger = logging.getLogger(__name__)
PROTOCOL_VERSION = 2


class ConversationService:
    """Question answering. Requests run as tasks of the instance that accepted them; that instance owns the
    request row (owner + lease_until, renewed while it runs). Events go through an event bus so a socket on
    another instance follows the request live; the committed row is the durable outcome (E3)."""
    def __init__(self, indexing, *, study, model=None, chat_model=None, events=None, instance=None):
        self.indexing, self.settings = indexing, indexing.settings
        self.store = ConversationStore(indexing.registry)
        self.contexts = ContextManager(self.store, self.settings, model)
        self.actions = ChatActions(indexing, study, model=chat_model)
        self.tasks = {}
        self.events = events or LocalEventBus()
        self.instance = instance
        self.shared = bool(instance) and self.settings.api_multi_instance
        self.workspace_of = None    # platform: video_id -> owning workspace (model scope, cache scope)
        self.heartbeat = None
        self.closed = False

    @property
    def subscribers(self):
        return self.events.subscribers

    def start(self):
        if self.shared:
            self.store.recover_expired()
            self.heartbeat = asyncio.get_event_loop().create_task(self._heartbeat())
        else:
            self.store.recover()

    async def _heartbeat(self):
        from vam.runtime import run_sync
        interval = max(1.0, self.settings.conversation_lease_s / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                if self.tasks:
                    await run_sync(self.store.renew, self.instance, self.settings.conversation_lease_s)
                # Any instance may interrupt requests whose owner died (lease expired): idempotent.
                await run_sync(self.store.recover_expired)
            except Exception:
                logger.warning('conversation_lease_heartbeat_failed')

    async def close(self):
        self.closed = True
        if self.heartbeat is not None:
            self.heartbeat.cancel()
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def envelope(self, event, cid, rid):
        row = self.store.get(cid)
        return event | {'protocol_version': PROTOCOL_VERSION, 'video_id': row['video_id'],
                        'conversation_id': cid, 'request_id': rid}

    def subscribe(self, cid, rid, queue):
        self.events.subscribe((cid, rid), queue)

    def unsubscribe(self, queue):
        self.events.unsubscribe(queue)

    def publish(self, cid, rid, event):
        self.events.publish((cid, rid), event)

    def submit(self, request):
        if self.closed:
            raise DomainError(503, 'server_stopping', 'Server is stopping')
        self.indexing._usable_video(request.video_id)
        self.actions.validate_scope(request.video_id, request.scope)
        cid, rid = request.conversation_id, request.request_id
        original = request.text if request.type == 'text' else (
            RetrieveToolArgs.model_validate(request.args).question if request.name == 'retrieve'
            else 'summarize:' + json.dumps(SummarizeToolArgs.model_validate(request.args).model_dump(), ensure_ascii=False))
        if len(original) > self.settings.conversation_message_chars:
            raise DomainError(413, 'question_too_large', 'Question exceeds the configured message limit')
        if not self.contexts.fits(original + ' ' * 900):
            raise DomainError(413, 'question_context_limit', 'Question exceeds the configured context budget')
        # Preserve the canonical payload of pre-shortcut requests on replay.
        payload = request.model_dump(exclude={'conversation_id', 'request_id', 'action', 'scope'})
        if request.action:
            payload['action'] = request.action
        if request.scope:
            payload['scope'] = request.scope.model_dump()
        fresh = self.store.accept(cid, rid, request.video_id, payload, original, self.settings.conversation_max_running,
                                  owner=self.instance if self.shared else None,
                                  lease_s=self.settings.conversation_lease_s if self.shared else None)
        if fresh:
            try:
                release = self.indexing._retain(request.video_id, purpose='question')
            except DomainError:
                self.store.fail(cid, rid, 'video_deleting')
                raise
            self.tasks[(cid, rid)] = asyncio.create_task(self._run(request, original, release))
        return self.store.request(cid, rid), fresh

    def terminal(self, row):
        cid, rid = row['conversation_id'], row['request_id']
        if row['status'] == 'completed':
            return self.store.mark_snapshot(row['result'], self.store.get(cid)['video_id'])
        if row['status'] in {'failed', 'interrupted'}:
            return self.envelope({'type': 'error', 'code': row['error_code'], 'status': row['status'],
                                  'message': 'Request did not complete; use a new request_id to retry'}, cid, rid)
        return None

    async def _run(self, request, original, release):
        from vam.gateway import model_scope
        from vam.runtime import run_sync
        cid, rid = request.conversation_id, request.request_id
        try:
            workspace = await run_sync(self.workspace_of, request.video_id) if self.workspace_of else None
            # Every model call of this question is 'interactive' and charged to the video's workspace.
            with model_scope(workspace, 'interactive'):
                await asyncio.wait_for(self._execute(request, original), self.settings.conversation_request_timeout_s)
        except asyncio.CancelledError:
            self.store.fail(cid, rid, 'server_shutdown', 'interrupted')
            self.publish(cid, rid, self.terminal(self.store.request(cid, rid)))
            raise
        except Exception as exc:
            code = 'request_timeout' if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else (exc.code if isinstance(exc, DomainError) else 'agent_failed')
            logger.warning('conversation_request_failed code=%s exception=%s', code, type(exc).__name__)
            self.store.fail(cid, rid, code)
            self.publish(cid, rid, self.terminal(self.store.request(cid, rid)))
        finally:
            await run_sync(release)
            self.tasks.pop((cid, rid), None)

    async def _execute(self, request, original):
        from vam.server.ws.agent_chat import scope_event
        from vam.retrieval.frame_store import FrameStore

        cid, rid = request.conversation_id, request.request_id
        context = ConversationContext('{}', [], []) if request.action in ('notes', 'transcript', 'slides') else await self.contexts.build(cid, original)
        if request.action or request.scope or request.type == 'tool' and request.name == 'summarize':
            resolution = {'original_question': original, 'rewritten_question': original, 'anchor_ids': [], 'needs_clarification': False}
        else:
            resolution = await self.contexts.resolve(original, context)
        self.store.resolved(cid, rid, resolution)
        self.publish(cid, rid, self.envelope({'type': 'resolved', 'resolution': resolution, 'warnings': context.warnings}, cid, rid))
        final = None
        if request.action in ('notes', 'transcript', 'slides'):
            final = self.actions.execute(request)
        elif not request.scope and re.search(r'这部分|这一部分|这一段', original):
            final = {'type': 'final', 'text': '请先选择一个章节或定位播放位置，明确你指的是哪一部分。',
                     'result': {'needs_clarification': True, 'evidence': [], 'citations': [], 'warnings': []}}
        elif resolution['needs_clarification']:
            final = {'type': 'final', 'text': resolution['clarification'],
                     'result': {'answer': resolution['clarification'], 'needs_clarification': True, 'evidence': [], 'citations': [], 'warnings': []}}
        elif request.type == 'text' and (self.actions.capabilities(request.video_id)['mode'] == 'text' or request.scope):
            self.publish(cid, rid, self.envelope({'type': 'status', 'stage': 'answering'}, cid, rid))
            final = await answer_text(self.actions, request, resolution, context)
        else:
            async with self.indexing.retrieval_context(request.video_id) as scope:
                store = scope.visual if scope.visual is not None else FrameStore(backend=scope.backend)
                history = context.history
                if request.type == 'text':
                    events = stream_agent_response(transcript=resolution['rewritten_question'], history=history,
                                                   store_instance=store, retrieval_context=scope, conversation_context=context)
                elif request.name == 'retrieve':
                    events = stream_retrieve_tool_request(transcript=resolution['rewritten_question'], history=history,
                                                           store_instance=store, retrieval_context=scope)
                else:
                    events = stream_summarize_tool_request(args=SummarizeToolArgs.model_validate(request.args), history=history,
                                                            store_instance=store, retrieval_context=scope)
                async for event in events:
                    event = scope_event(event, request.video_id, scope.row['active_generation'])
                    if event.get('type') == 'final':
                        final = event
                    else:
                        self.publish(cid, rid, self.envelope(event, cid, rid))
        if final is None:
            raise ValueError('Agent ended without final result')
        final = self.envelope(final | {'original_question': original, 'resolution': resolution,
                                     'context_warnings': context.warnings}, cid, rid)
        # Commit before publishing: failure to send cannot erase a completed answer.
        if self.store.finish(cid, rid, final):
            self.publish(cid, rid, final)
