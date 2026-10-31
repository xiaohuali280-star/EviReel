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
-- a per-process limiter, allowed only when this is the only process calling models (one API, no pipeline
workers); with several model-calling processes it is refused at API startup and ignored by workers.
Deadlines: the total timeout is absolute and bounds admission waits, backoff, reservation and each call;
the admission wait budget is shared by all attempts of one request.

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

    def _leave_wait(self, run):
        if run.waiting:
            run.waiting = False
            with self.waiters_lock:
                self.waiters -= 1

    def _reject(self, denied):
        self._count('rejected', denied.limit)
        raise DomainError(429, 'model_rate_limited', f'Model capacity is busy ({denied.limit}); retry in '
                          f'{max(1, round(denied.retry_after_s))} s')

    def _deadline_exceeded(self):
        self._count('rejected', 'total_deadline')
        return DomainError(504, 'model_deadline_exceeded', 'Model request exceeded its total time budget')

    # -- one policy for both execution styles ----------------------------------------------------------
    # The async and the sync (thread) entry points only differ in how they sleep and call the provider;
    # admission, deadlines, retries and accounting are these shared steps.
    def _start(self, timeout, attempts, total_timeout):
        lane = current_scope()[1]
        queue = self.settings.model_queue_wait_s if lane != 'offline' else self.settings.model_offline_queue_wait_s
        return _Run(timeout=timeout or self.settings.model_attempt_timeout_s,
                    attempts=attempts or self.settings.model_max_attempts,
                    deadline=time.monotonic() + (total_timeout or self.settings.model_total_timeout_s),
                    queue_left=queue)

    def _admit_step(self, run, call):
        """One admission try: ``(permit, limiter), 0`` when admitted, else ``None, pause`` to sleep before the
        next try. Never admits after the total deadline; the wait is bounded by the smaller of the remaining
        queue budget (shared by all attempts of the request) and the remaining total budget."""
        if time.monotonic() >= run.deadline:
            raise self._deadline_exceeded()
        result, limiter = self._try_admit(call)
        if not isinstance(result, Denied):
            self._count('admitted')
            return (result, limiter), 0
        self._count('denied', result.limit)
        remaining = run.deadline - time.monotonic()
        if run.queue_left <= 0:
            self._reject(result)
        if remaining <= 0:
            raise self._deadline_exceeded()
        if not run.waiting:
            if not self._enter_wait():
                self._reject(Denied(result.kind, 'max_waiters', result.retry_after_s))
            run.waiting = True
        pause = min(run.queue_left, remaining, max(.02, result.retry_after_s) * (0.5 + random.random() / 2), 2.0)
        run.queue_left -= pause
        return None, pause

    def _reserve(self, run, call, permit, limiter):
        try:
            self.ledger.reserve(call)
        except BaseException:
            limiter.release(permit)
            raise
        if time.monotonic() >= run.deadline:   # admission or the reservation used up the budget
            self.ledger.fail(call, 'deadline_exceeded')
            limiter.release(permit)
            raise self._deadline_exceeded()
        return min(run.timeout, run.deadline - time.monotonic())

    def _failed_attempt(self, run, call, permit, limiter, exc, number):
        """Record a failed provider attempt; the pause before the next one, or raise to stop."""
        (self.ledger.unknown if exc.sent else self.ledger.fail)(call, exc.code)
        self._count('outcomes', 'unknown' if exc.sent else 'failed')
        limiter.release(permit)
        remaining = run.deadline - time.monotonic()
        if not exc.retryable or number == run.attempts or remaining <= 0:
            raise exc
        self._count('retries')
        base = min(self.settings.model_retry_cap_s, self.settings.model_retry_base_s * 2 ** (number - 1))
        return min(max(exc.retry_after or 0, random.uniform(0, base)), remaining)

    def _unsent(self, call, permit, limiter):
        self.ledger.fail(call, 'cancelled_before_send')
        limiter.release(permit)

    def _caller_error(self, call, permit, limiter):
        self.ledger.unknown(call, 'caller_error')
        limiter.release(permit)

    def _succeeded(self, call, permit, limiter, usage):
        used_in = int((usage or {}).get('input_tokens') or 0)
        used_out = int((usage or {}).get('output_tokens') or 0)
        source = 'provider' if usage else 'estimate'
        if not usage:
            used_in, used_out = call.est_input_tokens, call.est_output_tokens
        self.ledger.settle(call, used_in, used_out, source)
        limiter.release(permit)
        limiter.adjust_tokens(permit, used_in + used_out - call.est_input_tokens - call.est_output_tokens)
        self._count('outcomes', 'settled')

    @staticmethod
    def _provider_error(exc):
        if isinstance(exc, (ProviderError, DomainError)):
            return None
        import httpx
        return from_httpx(exc) if isinstance(exc, httpx.HTTPError) else None

    # -- async ------------------------------------------------------------------------------------------
    async def run(self, *, operation, model, attempt_fn, est_input_tokens, est_output_tokens=0, provider=None,
                  timeout=None, attempts=None, total_timeout=None):
        """``attempt_fn()`` performs ONE provider request and returns ``(result, usage)`` where usage is
        ``{'input_tokens', 'output_tokens'}`` from the provider, or None. It raises ProviderError/httpx errors."""
        from vam.runtime import run_sync
        run = self._start(timeout, attempts, total_timeout)
        try:
            for number in range(1, run.attempts + 1):
                call = self._call(operation, model, provider, number, est_input_tokens, est_output_tokens, run.timeout)
                while True:
                    admitted, pause = await run_sync(self._admit_step, run, call)
                    if admitted:
                        break
                    await asyncio.sleep(pause)
                self._leave_wait(run)
                permit, limiter = admitted
                reserved = []

                def reserve():
                    reserved.append(self._reserve(run, call, permit, limiter))
                    return reserved[0]
                try:
                    budget = await run_sync(reserve)
                except asyncio.CancelledError:
                    # Cancelled while the reservation ran in its thread (run_sync waited for it): if it was
                    # granted, nothing was sent -- close it as failed and give the permit back.
                    if reserved:
                        await run_sync(self._unsent, call, permit, limiter)
                    raise
                try:
                    try:
                        result, usage = await asyncio.wait_for(attempt_fn(), budget)
                    except (TimeoutError, asyncio.TimeoutError):
                        raise ProviderError('provider_timeout', retryable=True, sent=True) from None
                    except Exception as exc:
                        mapped = self._provider_error(exc)
                        if mapped is None:
                            raise
                        raise mapped from exc
                except ProviderError as exc:
                    await asyncio.sleep(await run_sync(self._failed_attempt, run, call, permit, limiter, exc, number))
                    continue
                except BaseException:
                    await run_sync(self._caller_error, call, permit, limiter)
                    raise
                await run_sync(self._succeeded, call, permit, limiter, usage)
                return result
        finally:
            self._leave_wait(run)
        raise RuntimeError('model call failed')

    # -- sync (threads: the embedding client is synchronous) ------------------------------------------
    def run_sync(self, *, operation, model, attempt_fn, est_input_tokens, est_output_tokens=0, provider=None,
                 timeout=None, attempts=None, total_timeout=None):
        """Same policy; ``attempt_fn(timeout_s)`` must bound its own request by ``timeout_s``."""
        run = self._start(timeout, attempts, total_timeout)
        try:
            for number in range(1, run.attempts + 1):
                call = self._call(operation, model, provider, number, est_input_tokens, est_output_tokens, run.timeout)
                while True:
                    admitted, pause = self._admit_step(run, call)
                    if admitted:
                        break
                    time.sleep(pause)
                self._leave_wait(run)
                permit, limiter = admitted
                budget = self._reserve(run, call, permit, limiter)
                try:
                    try:
                        result, usage = attempt_fn(max(.1, budget))
                    except Exception as exc:
                        mapped = self._provider_error(exc)
                        if mapped is None:
                            raise
                        raise mapped from exc
                except ProviderError as exc:
                    time.sleep(self._failed_attempt(run, call, permit, limiter, exc, number))
                    continue
                except BaseException:
                    self._caller_error(call, permit, limiter)
                    raise
                self._succeeded(call, permit, limiter, usage)
                return result
        finally:
            self._leave_wait(run)
        raise RuntimeError('model call failed')


@dataclass
class _Run:
    """Budget of one gateway request (all its attempts)."""
    timeout: float
    attempts: int
    deadline: float          # absolute (monotonic): admission, backoff, reservation and calls all end here
    queue_left: float        # admission wait still allowed, shared by every attempt
    waiting: bool = False


def shares_model_limits(settings) -> bool:
    """True when other processes also call models: several API instances, or pipeline workers."""
    return bool(settings.api_multi_instance or settings.pipeline_mode == 'worker')


_gateway: ModelGateway | None = None
_gateway_lock = threading.Lock()


def build_gateway(settings, *, redis_client=None, db=None):
    if redis_client is not None:
        limiter = RedisLimiter(redis_client, settings)
        fallback = None
        if settings.model_limiter_on_redis_error == 'local':
            if shares_model_limits(settings):
                # Several model-calling processes (API instances, workers): no per-process limit can keep the
                # sum under the configured one, so this process rejects instead (the API refuses to start).
                logger.warning('model_limiter_local_fallback_ignored reason=shared_limits')
            else:
                fallback = LocalLimiter(settings)
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
