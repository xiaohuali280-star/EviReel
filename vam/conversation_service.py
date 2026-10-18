"""Bounded background executions; sockets subscribe without owning model lifetime."""
from __future__ import annotations

import asyncio
import json
import logging

from vam.agent import stream_agent_response, stream_retrieve_tool_request, stream_summarize_tool_request
from vam.memory.context_manager import ContextManager
from vam.memory.conversation import ConversationStore
from vam.protocol import RetrieveToolArgs, SummarizeToolArgs
from vam.video_registry import DomainError

logger = logging.getLogger(__name__)
PROTOCOL_VERSION = 2


class ConversationService:
    def __init__(self, indexing, *, model=None):
        self.indexing, self.settings = indexing, indexing.settings
        self.store = ConversationStore(indexing.registry)
        self.contexts = ContextManager(self.store, self.settings, model)
        self.tasks, self.subscribers = {}, {}
        self.closed = False

    def start(self):
        self.store.recover()

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def envelope(self, event, cid, rid):
        row = self.store.get(cid)
        return event | {'protocol_version': PROTOCOL_VERSION, 'video_id': row['video_id'],
                        'conversation_id': cid, 'request_id': rid}

    def subscribe(self, cid, rid, queue):
        self.subscribers.setdefault((cid, rid), set()).add(queue)

    def unsubscribe(self, queue):
        for key in list(self.subscribers):
            self.subscribers[key].discard(queue)
            if not self.subscribers[key]:
                del self.subscribers[key]

    def publish(self, cid, rid, event):
        for queue in self.subscribers.get((cid, rid), ()):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
        if event.get('type') in {'final', 'error'}:
            self.subscribers.pop((cid, rid), None)

    def submit(self, request):
        if self.closed:
            raise DomainError(503, 'server_stopping', 'Server is stopping')
        self.indexing._usable_video(request.video_id)
        cid, rid = request.conversation_id, request.request_id
        original = request.text if request.type == 'text' else (
            RetrieveToolArgs.model_validate(request.args).question if request.name == 'retrieve'
            else 'summarize:' + json.dumps(SummarizeToolArgs.model_validate(request.args).model_dump(), ensure_ascii=False))
        if len(original) > self.settings.conversation_message_chars:
            raise DomainError(413, 'question_too_large', 'Question exceeds the configured message limit')
        if not self.contexts.fits(original + ' ' * 900):
            raise DomainError(413, 'question_context_limit', 'Question exceeds the configured context budget')
        payload = request.model_dump(exclude={'conversation_id', 'request_id'})
        fresh = self.store.accept(cid, rid, request.video_id, payload, original, self.settings.conversation_max_running)
        if fresh:
            release = self.indexing._retain(request.video_id)
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
        cid, rid = request.conversation_id, request.request_id
        try:
            await asyncio.wait_for(self._execute(request, original), self.settings.conversation_request_timeout_s)
        except asyncio.CancelledError:
            self.store.fail(cid, rid, 'server_shutdown', 'interrupted')
            self.publish(cid, rid, self.terminal(self.store.request(cid, rid)))
            raise
        except Exception as exc:
            code = 'request_timeout' if isinstance(exc, TimeoutError) else (exc.code if isinstance(exc, DomainError) else 'agent_failed')
            logger.warning('conversation_request_failed code=%s exception=%s', code, type(exc).__name__)
            self.store.fail(cid, rid, code)
            self.publish(cid, rid, self.terminal(self.store.request(cid, rid)))
        finally:
            release()
            self.tasks.pop((cid, rid), None)

    async def _execute(self, request, original):
        from vam.server.ws.agent_chat import scope_event
        from vam.retrieval.frame_store import FrameStore

        cid, rid = request.conversation_id, request.request_id
        context = await self.contexts.build(cid, original)
        if request.type == 'tool' and request.name == 'summarize':
            resolution = {'original_question': original, 'rewritten_question': original, 'anchor_ids': [], 'needs_clarification': False}
        else:
            resolution = await self.contexts.resolve(original, context)
        self.store.resolved(cid, rid, resolution)
        self.publish(cid, rid, self.envelope({'type': 'resolved', 'resolution': resolution, 'warnings': context.warnings}, cid, rid))
        final = None
        if resolution['needs_clarification']:
            final = {'type': 'final', 'text': resolution['clarification'],
                     'result': {'answer': resolution['clarification'], 'needs_clarification': True, 'evidence': [], 'citations': [], 'warnings': []}}
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
