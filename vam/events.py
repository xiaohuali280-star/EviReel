"""Live notifications for in-flight question answering (E3), separate from durable state.

The durable record is the request row (accepted -> running -> completed/failed/interrupted) in the database;
it is committed *before* the final event is published. Notifications only make the stream live:

* ``LocalEventBus`` -- one API instance: delivers to this process's subscribers.
* ``RedisEventBus`` -- several API instances: every event is delivered locally at once and published on
  Redis Pub/Sub; each instance's listener delivers remote events to its own subscribers (own events are
  recognised by origin and not delivered twice). A socket connected to instance B therefore receives the
  progress of a request executing on instance A.

Pub/Sub keeps no history: an event published while a client is disconnected is lost. Clients recover by
reading the request snapshot (WebSocket replay of the terminal row, or ``GET .../requests/{id}``); this is
not an event replay and intermediate progress events are not recoverable.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging

logger = logging.getLogger(__name__)
CHANNEL = 'evireel:events:'


class LocalEventBus:
    shared = False

    def __init__(self):
        self.subscribers: dict[tuple, set] = {}

    def subscribe(self, key, queue):
        self.subscribers.setdefault(key, set()).add(queue)

    def unsubscribe(self, queue):
        for key in list(self.subscribers):
            self.subscribers[key].discard(queue)
            if not self.subscribers[key]:
                del self.subscribers[key]

    def deliver(self, key, event):
        for queue in list(self.subscribers.get(key, ())):
            if queue.full():
                queue.get_nowait()        # bounded per-socket buffer: drop the oldest progress event
            queue.put_nowait(event)
        if event.get('type') in {'final', 'error'}:
            self.subscribers.pop(key, None)

    def publish(self, key, event):
        self.deliver(key, event)

    async def start(self):
        pass

    async def close(self):
        pass


class RedisEventBus(LocalEventBus):
    shared = True

    def __init__(self, client, origin):
        super().__init__()
        self.client, self.origin = client, origin
        self.listener = None
        self.published, self.publish_errors, self.received = 0, 0, 0

    @staticmethod
    def channel(key):
        return CHANNEL + ':'.join(key)

    def publish(self, key, event):
        self.deliver(key, event)
        try:
            self.client.publish(self.channel(key), json.dumps({'origin': self.origin, 'key': list(key), 'event': event},
                                                              ensure_ascii=False))
            self.published += 1
        except Exception:
            # Remote sockets miss live progress; they still get the committed terminal row on reconnect/poll.
            self.publish_errors += 1
            logger.warning('event_publish_failed key=%s', key)

    async def start(self):
        async def listen():
            from vam.runtime import run_sync
            while True:
                pubsub = None
                try:
                    pubsub = self.client.pubsub(ignore_subscribe_messages=True)
                    await run_sync(pubsub.psubscribe, CHANNEL + '*')
                    while True:
                        message = await run_sync(pubsub.get_message, timeout=1.0)
                        if not message:
                            continue
                        data = json.loads(message['data'])
                        if data.get('origin') == self.origin:
                            continue
                        self.received += 1
                        self.deliver(tuple(data['key']), data['event'])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning('event_listener_reconnect')
                    await asyncio.sleep(1)
                finally:
                    if pubsub is not None:
                        with contextlib.suppress(Exception):
                            pubsub.close()
        self.listener = asyncio.create_task(listen())

    async def close(self):
        if self.listener is not None:
            self.listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.listener
