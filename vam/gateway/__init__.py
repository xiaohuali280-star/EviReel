"""ModelGateway (E3): the single path every provider call takes -- chat/VLM, embeddings, rerank, study notes.

Per *attempt* (retries are separate attempts and pay again):
  1. admission  -- limiter.acquire: global/provider/workspace rates, provider TPM, in-flight slots with a
                   lease; offline work has its own smaller slot cap so interactive questions keep capacity.
                   Denied -> wait (bounded by MODEL_QUEUE_WAIT_S and MODEL_MAX_WAITERS) or reject with 429.
  2. reservation -- ledger.reserve: daily token budget check + a 'reserved' usage row (PostgreSQL).
  3. call        -- bounded by a per-attempt timeout; the overall run is bounded by a total deadline.
  4. settlement  -- settled (provider usage) / failed (certainly not billed) / unknown (maybe billed).
  5. release     -- the slot is released; if the provider reported more tokens than estimated, the TPM
                   window is charged the difference.
Retries: 429/5xx/timeouts/transport errors, exponential backoff with full jitter, honouring Retry-After.
Shared limiter unreachable: MODEL_LIMITER_ON_REDIS_ERROR=reject (default: 503, no call is made) or local
(per-process limiter with every limit divided by API_INSTANCE_COUNT -- conservative, not shared).

Scope (workspace, lane) comes from a ContextVar set where work starts: question answering and search are
'interactive', pipeline stages and note generation are 'offline'.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from vam.gateway.ledger import NullLedger, PgLedger
from vam.gateway.limiter import Denied, LimiterUnavailable, LocalLimiter, RedisLimiter
from vam.video_registry import DomainError

logger = logging.getLogger(__name__)
_scope: ContextVar[tuple] = ContextVar('evireel_model_scope', default=(None, 'interactive'))


@contextlib.contextmanager
def model_scope(workspace, lane):
    token = _scope.set((workspace, lane))
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope():
    return _scope.get()


class ProviderError(Exception):
    """One failed provider attempt. ``sent`` = the request may have reached the provider (billing unknown)."""
    def __init__(self, code, *, status=None, retryable=False, sent=True, retry_after=None):
        super().__init__(code)
        self.code, self.status, self.retryable, self.sent, self.retry_after = code, status, retryable, sent, retry_after


def from_httpx(exc):
    """Classify an httpx exception for retry and billing decisions."""
    import httpx
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retry_after = None
        with contextlib.suppress(ValueError, TypeError):
            retry_after = float(exc.response.headers.get('retry-after'))
        return ProviderError(f'provider_http_{status}', status=status, retryable=status in (408, 409, 425, 429, 500, 502, 503, 504),
                             sent=status >= 500, retry_after=retry_after)
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return ProviderError('provider_connect_failed', retryable=True, sent=False)
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError('provider_timeout', retryable=True, sent=True)
    return ProviderError('provider_transport_error', retryable=True, sent=True)


@dataclass
class ModelCall:
    call_id: str
    workspace: str
    provider: str
    model: str
    operation: str
    lane: str
    attempt: int
    est_input_tokens: int
    est_output_tokens: int
    deadline_s: float


def estimate_tokens(text_chars: int) -> int:
    """Conservative token estimate before the call: ~1 token per 2 characters (CJK-heavy text) + overhead."""
    return max(1, int(text_chars) // 2 + 16)


class ModelGateway:
    def __init__(self, settings, limiter, ledger, *, fallback=None):
        self.settings, self.limiter, self.ledger, self.fallback = settings, limiter, ledger, fallback
        self.waiters = 0
        self.waiters_lock = threading.Lock()
        self.stats = {'admitted': 0, 'denied': {}, 'rejected': {}, 'retries': 0, 'outcomes': {}, 'limiter_errors': 0}
        self.stats_lock = threading.Lock()

    # -- bookkeeping --------------------------------------------------------------------------------
    def _count(self, group, key=None):
        with self.stats_lock:
            if key is None:
                self.stats[group] += 1
            else:
                self.stats[group][key] = self.stats[group].get(key, 0) + 1

    def snapshot(self):
        with self.stats_lock:
            data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.stats.items()}
        data['limiter'] = self.limiter.name
        data['ledger_persistent'] = self.ledger.persistent
        data['waiters'] = self.waiters
        return data

    def _call(self, operation, model, provider, attempt, est_in, est_out, timeout):
        workspace, lane = current_scope()
        return ModelCall(str(uuid.uuid4()), workspace or 'unscoped', provider or self.settings.provider_name,
                         model or 'unknown', operation, lane if lane in ('interactive', 'offline') else 'interactive',
                         attempt, int(est_in), int(est_out), timeout + self.settings.model_slot_grace_s)

    def _try_admit(self, call):
        permit_id = call.call_id
        lease_ms = call.deadline_s * 1000
        tokens = call.est_input_tokens + call.est_output_tokens
        try:
            return self.limiter.acquire(provider=call.provider, workspace=call.workspace, lane=call.lane,
                                        tokens=tokens, permit_id=permit_id, lease_ms=lease_ms), self.limiter
        except LimiterUnavailable:
            self._count('limiter_errors')
            if self.fallback is None:
                raise DomainError(503, 'model_limiter_unavailable',
                                  'Model rate limiting is unavailable; model calls are paused') from None
            return self.fallback.acquire(provider=call.provider, workspace=call.workspace, lane=call.lane,
                                         tokens=tokens, permit_id=permit_id, lease_ms=lease_ms), self.fallback

    def _enter_wait(self):
        with self.waiters_lock:
            if self.waiters >= self.settings.model_max_waiters:
                return False
            self.waiters += 1
            return True

    def _leave_wait(self):
        with self.waiters_lock:
            self.waiters -= 1

    def _wait_budget(self, call):
        return self.settings.model_queue_wait_s if call.lane == 'interactive' else self.settings.model_offline_queue_wait_s

    def _reject(self, denied):
        self._count('rejected', denied.limit)
        raise DomainError(429, 'model_rate_limited', f'Model capacity is busy ({denied.limit}); retry in '
                          f'{max(1, round(denied.retry_after_s))} s')

    # -- async ------------------------------------------------------------------------------------------
    async def _admit(self, call):
        from vam.runtime import run_sync
        waited, registered = 0.0, False
        try:
            while True:
                result, limiter = await run_sync(self._try_admit, call)
                if not isinstance(result, Denied):
                    self._count('admitted')
                    return result, limiter
                self._count('denied', result.limit)
                budget = self._wait_budget(call) - waited
                if budget <= 0:
                    self._reject(result)
                if not registered:
                    if not self._enter_wait():
                        self._reject(Denied(result.kind, 'max_waiters', result.retry_after_s))
                    registered = True
                pause = min(budget, max(.02, result.retry_after_s) * (0.5 + random.random() / 2), 2.0)
                await asyncio.sleep(pause)
                waited += pause
        finally:
            if registered:
                self._leave_wait()

    async def run(self, *, operation, model, attempt_fn, est_input_tokens, est_output_tokens=0, provider=None,
                  timeout=None, attempts=None, total_timeout=None):
        """``attempt_fn()`` performs ONE provider request and returns ``(result, usage)`` where usage is
        ``{'input_tokens', 'output_tokens'}`` from the provider, or None. It raises ProviderError/httpx errors."""
        from vam.runtime import run_sync
        timeout = timeout or self.settings.model_attempt_timeout_s
        attempts = attempts or self.settings.model_max_attempts
        deadline = time.monotonic() + (total_timeout or self.settings.model_total_timeout_s)
        last = None
        for number in range(1, attempts + 1):
            call = self._call(operation, model, provider, number, est_input_tokens, est_output_tokens, timeout)
            permit, limiter = await self._admit(call)
            try:
                await run_sync(self.ledger.reserve, call)
            except BaseException:
                limiter.release(permit)
                raise
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await run_sync(self.ledger.fail, call, 'deadline_exceeded')
                    raise DomainError(504, 'model_deadline_exceeded', 'Model request exceeded its total time budget')
                try:
                    result, usage = await asyncio.wait_for(attempt_fn(), min(timeout, remaining))
                except (TimeoutError, asyncio.TimeoutError):
                    raise ProviderError('provider_timeout', retryable=True, sent=True) from None
                except ProviderError:
                    raise
                except DomainError:
                    raise
                except Exception as exc:
                    import httpx
                    if isinstance(exc, httpx.HTTPError):
                        raise from_httpx(exc) from exc
                    raise
            except ProviderError as exc:
                last = exc
                await run_sync(self.ledger.unknown if exc.sent else self.ledger.fail, call, exc.code)
                self._count('outcomes', 'unknown' if exc.sent else 'failed')
                limiter.release(permit)
                if not exc.retryable or number == attempts or time.monotonic() >= deadline:
                    raise
                self._count('retries')
                base = min(self.settings.model_retry_cap_s, self.settings.model_retry_base_s * 2 ** (number - 1))
                pause = max(exc.retry_after or 0, random.uniform(0, base))
                await asyncio.sleep(min(pause, max(0, deadline - time.monotonic())))
                continue
            except BaseException:
                await run_sync(self.ledger.unknown, call, 'caller_error')
                limiter.release(permit)
                raise
            used_in = int((usage or {}).get('input_tokens') or 0)
            used_out = int((usage or {}).get('output_tokens') or 0)
            source = 'provider' if usage else 'estimate'
            if not usage:
                used_in, used_out = call.est_input_tokens, call.est_output_tokens
            await run_sync(self.ledger.settle, call, used_in, used_out, source)
            limiter.release(permit)
            limiter.adjust_tokens(permit, used_in + used_out - call.est_input_tokens - call.est_output_tokens)
            self._count('outcomes', 'settled')
            return result
        raise last or RuntimeError('model call failed')

    # -- sync (threads: the embedding client is synchronous) ------------------------------------------
    def run_sync(self, *, operation, model, attempt_fn, est_input_tokens, est_output_tokens=0, provider=None,
                 timeout=None, attempts=None, total_timeout=None):
        timeout = timeout or self.settings.model_attempt_timeout_s
        attempts = attempts or self.settings.model_max_attempts
        deadline = time.monotonic() + (total_timeout or self.settings.model_total_timeout_s)
        last = None
        for number in range(1, attempts + 1):
            call = self._call(operation, model, provider, number, est_input_tokens, est_output_tokens, timeout)
            waited, registered = 0.0, False
            try:
                while True:
                    permit, limiter = self._try_admit(call)
                    if not isinstance(permit, Denied):
                        self._count('admitted')
                        break
                    self._count('denied', permit.limit)
                    budget = self._wait_budget(call) - waited
                    if budget <= 0:
                        self._reject(permit)
                    if not registered:
                        if not self._enter_wait():
                            self._reject(Denied(permit.kind, 'max_waiters', permit.retry_after_s))
                        registered = True
                    pause = min(budget, max(.02, permit.retry_after_s) * (0.5 + random.random() / 2), 2.0)
                    time.sleep(pause)
                    waited += pause
            finally:
                if registered:
                    self._leave_wait()
            try:
                self.ledger.reserve(call)
            except BaseException:
                limiter.release(permit)
                raise
            try:
                if time.monotonic() >= deadline:
                    self.ledger.fail(call, 'deadline_exceeded')
                    raise DomainError(504, 'model_deadline_exceeded', 'Model request exceeded its total time budget')
                try:
                    result, usage = attempt_fn(min(timeout, max(.1, deadline - time.monotonic())))
                except ProviderError:
                    raise
                except DomainError:
                    raise
                except Exception as exc:
                    import httpx
                    if isinstance(exc, httpx.HTTPError):
                        raise from_httpx(exc) from exc
                    raise
            except ProviderError as exc:
                last = exc
                (self.ledger.unknown if exc.sent else self.ledger.fail)(call, exc.code)
                self._count('outcomes', 'unknown' if exc.sent else 'failed')
                limiter.release(permit)
                if not exc.retryable or number == attempts or time.monotonic() >= deadline:
                    raise
                self._count('retries')
                base = min(self.settings.model_retry_cap_s, self.settings.model_retry_base_s * 2 ** (number - 1))
                time.sleep(min(max(exc.retry_after or 0, random.uniform(0, base)), max(0, deadline - time.monotonic())))
                continue
            except BaseException:
                self.ledger.unknown(call, 'caller_error')
                limiter.release(permit)
                raise
            used_in = int((usage or {}).get('input_tokens') or 0)
            used_out = int((usage or {}).get('output_tokens') or 0)
            source = 'provider' if usage else 'estimate'
            if not usage:
                used_in, used_out = call.est_input_tokens, call.est_output_tokens
            self.ledger.settle(call, used_in, used_out, source)
            limiter.release(permit)
            limiter.adjust_tokens(permit, used_in + used_out - call.est_input_tokens - call.est_output_tokens)
            self._count('outcomes', 'settled')
            return result
        raise last or RuntimeError('model call failed')


_gateway: ModelGateway | None = None
_gateway_lock = threading.Lock()


def build_gateway(settings, *, redis_client=None, db=None):
    if redis_client is not None:
        limiter = RedisLimiter(redis_client, settings)
        fallback = LocalLimiter(settings, share=settings.api_instance_count) \
            if settings.model_limiter_on_redis_error == 'local' else None
    else:
        limiter, fallback = LocalLimiter(settings), None
    ledger = PgLedger(db, settings) if db is not None else NullLedger()
    return ModelGateway(settings, limiter, ledger, fallback=fallback)


def configure(gateway: ModelGateway | None):
    global _gateway
    with _gateway_lock:
        _gateway = gateway


def get_gateway() -> ModelGateway:
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            from vam.config import get_settings
            _gateway = build_gateway(get_settings())
        return _gateway
