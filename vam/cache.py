"""Versioned cache with request coalescing (E3).

What is cached: query embeddings and per-channel retrieval candidates -- never answers. Every key contains
the authorization scope (the owning workspace), the pinned source version, the model identity and the
retrieval parameters, so a publication or model change reads new keys and old keys simply age out. Lookups
happen inside an already authorized request (the middleware / authorize_video ran first).

Coalescing (single flight): the first caller takes a short lock with a random token and computes; the
others poll for the value. The value is written only if the writer still owns the lock (compare token in
Lua), so an executor that outlived its lock (slow or paused) cannot overwrite a newer computation. A failed
computation releases the lock and caches nothing; waiters then compute themselves. TTLs carry +-jitter so
hot keys do not expire together.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid
from collections import OrderedDict

SET_IF_OWNER = """
if redis.call('GET', KEYS[2]) == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
  redis.call('DEL', KEYS[2])
  return 1
end
return 0
"""
DEL_IF_OWNER = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def cache_key(kind, *parts):
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return f'evireel:cache:{kind}:{digest}'


class _Stats:
    def __init__(self):
        self.hits = self.misses = self.coalesced = self.computed = self.errors = self.stale_writes = 0

    def as_dict(self):
        return dict(vars(self))


class LocalCache:
    """In-process LRU with the same contract (single instance or no Redis)."""
    name = 'local'

    def __init__(self, settings, *, max_items=2048):
        self.settings, self.max_items = settings, max_items
        self.items: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self.inflight: dict[str, asyncio.Future] = {}
        self.stats = _Stats()

    def _ttl(self, ttl):
        return ttl * (1 + random.uniform(-self.settings.cache_ttl_jitter, self.settings.cache_ttl_jitter))

    async def get_or_compute(self, key, compute, *, ttl):
        entry = self.items.get(key)
        if entry and entry[0] > time.monotonic():
            self.items.move_to_end(key)
            self.stats.hits += 1
            return entry[1]
        if key in self.inflight:
            self.stats.coalesced += 1
            return await asyncio.shield(self.inflight[key])
        self.stats.misses += 1
        future = asyncio.get_running_loop().create_future()
        self.inflight[key] = future
        try:
            value = await compute()
        except BaseException as exc:
            self.stats.errors += 1
            future.set_exception(exc)
            future.exception()      # mark retrieved: waiters re-raise, nothing is cached
            raise
        finally:
            self.inflight.pop(key, None)
        self.stats.computed += 1
        future.set_result(value)
        self.items[key] = (time.monotonic() + self._ttl(ttl), value)
        while len(self.items) > self.max_items:
            self.items.popitem(last=False)
        return value


class RedisCache:
    name = 'redis'

    def __init__(self, client, settings):
        self.client, self.settings = client, settings
        self._set_if_owner = client.register_script(SET_IF_OWNER)
        self._del_if_owner = client.register_script(DEL_IF_OWNER)
        self.stats = _Stats()

    def _ttl_ms(self, ttl):
        return int(1000 * ttl * (1 + random.uniform(-self.settings.cache_ttl_jitter, self.settings.cache_ttl_jitter)))

    async def get_or_compute(self, key, compute, *, ttl):
        from vam.runtime import run_sync
        lock_key = key + ':lock'
        lock_ms = int(self.settings.cache_lock_s * 1000)
        deadline = time.monotonic() + self.settings.cache_lock_s
        try:
            while True:
                raw = await run_sync(self.client.get, key)
                if raw is not None:
                    self.stats.hits += 1
                    return json.loads(raw)
                token = uuid.uuid4().hex
                if await run_sync(lambda: self.client.set(lock_key, token, nx=True, px=lock_ms)):
                    break
                if time.monotonic() >= deadline:   # the holder is slow or gone: compute without caching
                    self.stats.misses += 1
                    return await compute()
                self.stats.coalesced += 1
                await asyncio.sleep(.03 + random.random() * .03)
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            self.stats.errors += 1           # Redis unavailable: the cache is an optimisation only
            return await compute()
        self.stats.misses += 1
        try:
            value = await compute()
        except BaseException:
            try:
                await run_sync(self._del_if_owner, keys=[lock_key], args=[token])
            except Exception:
                pass
            raise
        self.stats.computed += 1
        try:
            written = await run_sync(self._set_if_owner, keys=[key, lock_key],
                                     args=[token, json.dumps(value), self._ttl_ms(ttl)])
            if not int(written):
                self.stats.stale_writes += 1     # lock expired under us: another executor owns the key
        except Exception:
            self.stats.errors += 1
        return value
