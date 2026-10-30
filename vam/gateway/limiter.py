"""Admission control for model calls: shared rate windows + leased in-flight slots (E3).

One ``acquire`` admits a call only if *every* applicable limit has room, and then charges all of them --
atomically. With Redis this is one Lua script, so N API instances and the Celery workers share one budget
(no instance can admit its own copy of the limit). Limits checked per call:

  rate  global RPM, provider RPM, provider TPM (estimated tokens), workspace RPM
  slot  provider in-flight, workspace in-flight, and for the offline lane an offline in-flight cap that is
        smaller than the provider total -- the difference is reserved for interactive questions.

Rates use a sliding-window counter (current minute + the previous minute weighted by the unexpired part).
Slots are sorted-set members scored by their lease expiry: a holder that dies without releasing stops
counting when its lease expires (lease = call timeout + grace; calls are bounded by that timeout).
Clock: Redis ``TIME`` inside the script, so instance clocks do not matter.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local window = math.floor(now / 60000)
local elapsed = (now % 60000) / 60000
local nrates = tonumber(ARGV[1])
local i = 2
for r = 1, nrates do
  local limit, cost = tonumber(ARGV[i]), tonumber(ARGV[i + 1])
  i = i + 2
  if limit >= 0 then
    local curr = tonumber(redis.call('GET', KEYS[r] .. ':' .. window) or '0')
    local prev = tonumber(redis.call('GET', KEYS[r] .. ':' .. (window - 1)) or '0')
    if prev * (1 - elapsed) + curr + cost > limit then
      return {0, 'rate', r, 60000 - (now % 60000)}
    end
  end
end
local nslots = tonumber(ARGV[i])
i = i + 1
for s = 1, nslots do
  local key, limit = KEYS[nrates + s], tonumber(ARGV[i])
  i = i + 1
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
  if limit >= 0 and redis.call('ZCARD', key) >= limit then
    local first = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    return {0, 'slot', s, math.max(50, tonumber(first[2] or now) - now)}
  end
end
local permit, ttl = ARGV[i], tonumber(ARGV[i + 1])
i = 2
for r = 1, nrates do
  local key = KEYS[r] .. ':' .. window
  redis.call('INCRBY', key, tonumber(ARGV[i + 1]))
  redis.call('PEXPIRE', key, 180000)
  i = i + 2
end
for s = 1, nslots do
  redis.call('ZADD', KEYS[nrates + s], now + ttl, permit)
  redis.call('PEXPIRE', KEYS[nrates + s], ttl + 60000)
end
return {1, 'ok', 0, 0}
"""

RELEASE = """
for k = 1, #KEYS do redis.call('ZREM', KEYS[k], ARGV[1]) end
return 1
"""

ADJUST = """
local t = redis.call('TIME')
local window = math.floor((tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)) / 60000)
redis.call('INCRBY', KEYS[1] .. ':' .. window, tonumber(ARGV[1]))
redis.call('PEXPIRE', KEYS[1] .. ':' .. window, 180000)
return 1
"""


class LimiterUnavailable(RuntimeError):
    """The shared limiter cannot be reached; the gateway applies the configured failure policy."""


@dataclass
class Denied:
    kind: str          # 'rate' | 'slot'
    limit: str         # which limit (name)
    retry_after_s: float


@dataclass
class Permit:
    permit_id: str
    slot_keys: list = field(default_factory=list)
    tpm_key: str | None = None
    backend: str = 'redis'


def plan(settings, *, provider, workspace, lane, tokens):
    """Rate keys/limits/costs and slot keys/limits for one call (``-1`` disables a limit)."""
    p, w = provider, workspace or 'unscoped'
    rates = [('global_rpm', 'evireel:rl:global', settings.model_rpm_global, 1),
             ('provider_rpm', f'evireel:rl:p:{p}', settings.model_rpm_provider, 1),
             ('provider_tpm', f'evireel:tl:p:{p}', settings.model_tpm_provider, max(1, int(tokens))),
             ('workspace_rpm', f'evireel:rl:w:{w}', settings.model_rpm_workspace, 1)]
    slots = [('provider_inflight', f'evireel:sl:p:{p}', settings.model_inflight_provider),
             ('workspace_inflight', f'evireel:sl:w:{w}', settings.model_inflight_workspace)]
    if lane == 'offline':
        slots.append(('offline_inflight', f'evireel:sl:p:{p}:offline', settings.model_inflight_offline))
    return rates, slots


class RedisLimiter:
    name = 'redis'

    def __init__(self, client, settings):
        self.client, self.settings = client, settings
        self._acquire = client.register_script(ACQUIRE)
        self._release = client.register_script(RELEASE)
        self._adjust = client.register_script(ADJUST)

    def acquire(self, *, provider, workspace, lane, tokens, permit_id, lease_ms):
        rates, slots = plan(self.settings, provider=provider, workspace=workspace, lane=lane, tokens=tokens)
        args = [len(rates)]
        for _, _, limit, cost in rates:
            args += [limit, cost]
        args.append(len(slots))
        args += [limit for _, _, limit in slots]
        args += [permit_id, int(lease_ms)]
        try:
            ok, kind, index, retry_ms = self._acquire(keys=[k for _, k, _, _ in rates] + [k for _, k, _ in slots], args=args)
        except Exception as exc:     # connection refused, timeout, READONLY, script errors...
            raise LimiterUnavailable(type(exc).__name__) from exc
        kind = kind.decode() if isinstance(kind, bytes) else kind
        if not int(ok):
            names = [n for n, *_ in rates] if kind == 'rate' else [n for n, *_ in slots]
            return Denied(kind, names[int(index) - 1], int(retry_ms) / 1000)
        return Permit(permit_id, [k for _, k, _ in slots], rates[2][1], 'redis')

    def release(self, permit):
        try:
            self._release(keys=permit.slot_keys, args=[permit.permit_id])
        except Exception:
            pass   # the slot lease expires on its own (lease = timeout + grace)

    def adjust_tokens(self, permit, delta):
        if delta > 0 and permit.tpm_key:
            try:
                self._adjust(keys=[permit.tpm_key], args=[int(delta)])
            except Exception:
                pass


class LocalLimiter:
    """Same algorithm in one process: for single-instance deployments and as the explicit
    ``MODEL_LIMITER_ON_REDIS_ERROR=local`` fallback (limits divided by the configured instance count)."""
    name = 'local'

    def __init__(self, settings, *, share=1):
        self.settings, self.share = settings, max(1, int(share))
        self.lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.slots: dict[str, dict[str, float]] = {}

    def _scaled(self, limit):
        return limit if limit < 0 else max(1, limit // self.share)

    def acquire(self, *, provider, workspace, lane, tokens, permit_id, lease_ms):
        rates, slots = plan(self.settings, provider=provider, workspace=workspace, lane=lane, tokens=tokens)
        now = time.time() * 1000
        window, elapsed = int(now // 60000), (now % 60000) / 60000
        with self.lock:
            for name, key, limit, cost in rates:
                limit = self._scaled(limit)
                if limit >= 0:
                    estimate = self.counts.get(f'{key}:{window - 1}', 0) * (1 - elapsed) + self.counts.get(f'{key}:{window}', 0)
                    if estimate + cost > limit:
                        return Denied('rate', name, (60000 - now % 60000) / 1000)
            for name, key, limit in slots:
                limit = self._scaled(limit)
                held = {p: exp for p, exp in self.slots.get(key, {}).items() if exp > now}
                self.slots[key] = held
                if limit >= 0 and len(held) >= limit:
                    return Denied('slot', name, max(.05, (min(held.values()) - now) / 1000))
            for name, key, limit, cost in rates:
                self.counts[f'{key}:{window}'] = self.counts.get(f'{key}:{window}', 0) + cost
            for name, key, limit in slots:
                self.slots[key][permit_id] = now + lease_ms
            for stale in [k for k in self.counts if int(k.rsplit(':', 1)[1]) < window - 1]:
                del self.counts[stale]
        return Permit(permit_id, [k for _, k, _ in slots], rates[2][1], 'local')

    def release(self, permit):
        with self.lock:
            for key in permit.slot_keys:
                self.slots.get(key, {}).pop(permit.permit_id, None)

    def adjust_tokens(self, permit, delta):
        if delta > 0 and permit.tpm_key:
            window = int(time.time() // 60)
            with self.lock:
                key = f'{permit.tpm_key}:{window}'
                self.counts[key] = self.counts.get(key, 0) + int(delta)
