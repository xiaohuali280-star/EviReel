"""E3 units without middleware: registry SQL translation, limiter algorithm, gateway retry/failure policy,
in-process single-flight cache, local notifications. Real Redis/PostgreSQL behaviour is in
tests/test_e3_redis.py and tests/test_e3_platform.py; nothing here stands in for them."""
from __future__ import annotations

import asyncio
import threading

import pytest

from vam.config import Settings


def settings(**overrides):
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------- registry translation
def test_registry_statements_translate_to_postgres():
    from vam.storage.pg_registry import translate
    assert translate("SELECT * FROM videos WHERE video_id=? AND status IN ('a','b')") == \
        "SELECT * FROM videos WHERE video_id=%s AND status IN ('a','b')"
    assert translate("INSERT OR IGNORE INTO study_jobs VALUES(?,?)") == "INSERT INTO study_jobs VALUES(%s,%s) ON CONFLICT DO NOTHING"
    assert translate("SELECT 1 WHERE instr(lower(original_name),lower(?))>0") == "SELECT 1 WHERE strpos(lower(original_name),lower(%s))>0"
    assert translate("video_id IN (SELECT value FROM json_each(?))") == "video_id IN (SELECT jsonb_array_elements_text(%s::jsonb))"
    assert translate("SELECT '100%' AS x") == "SELECT '100%%' AS x"     # literal percent stays literal


def test_registry_rows_behave_like_sqlite_rows():
    from vam.storage.pg_registry import Row
    row = Row(["video_id", "status"], ("v_1", "ready"))
    assert row[0] == "v_1" and row["status"] == "ready" and dict(row) == {"video_id": "v_1", "status": "ready"}
    assert tuple(row) == ("v_1", "ready") and row.keys() == ["video_id", "status"]
    with pytest.raises(IndexError):
        row["missing"]


def test_multi_instance_requires_the_shared_registry(tmp_path):
    from fastapi.testclient import TestClient
    from vam.server.app import create_app
    s = settings(data_root=str(tmp_path), api_multi_instance=True)
    with pytest.raises(RuntimeError, match="API_MULTI_INSTANCE"):
        with TestClient(create_app(settings=s)):
            pass


# ---------------------------------------------------------------------------- limiter algorithm
def acquire(limiter, n, *, lane="interactive", workspace="ws", tokens=10):
    from vam.gateway.limiter import Permit
    out = []
    for i in range(n):
        out.append(limiter.acquire(provider="p", workspace=workspace, lane=lane, tokens=tokens,
                                   permit_id=f"{lane}-{workspace}-{i}-{id(out)}", lease_ms=60_000))
    return [o for o in out if isinstance(o, Permit)], [o for o in out if not isinstance(o, Permit)]


def test_offline_lane_cannot_take_the_capacity_reserved_for_questions():
    from vam.gateway.limiter import LocalLimiter
    s = settings(model_inflight_provider=4, model_inflight_offline=2, model_inflight_workspace=-1,
                 model_rpm_global=-1, model_rpm_provider=-1, model_tpm_provider=-1, model_rpm_workspace=-1)
    limiter = LocalLimiter(s)
    offline, denied = acquire(limiter, 5, lane="offline")
    assert len(offline) == 2 and denied[0].limit == "offline_inflight"
    interactive, denied = acquire(limiter, 5)
    assert len(interactive) == 2 and denied[0].limit == "provider_inflight"
    limiter.release(interactive[0])
    assert len(acquire(limiter, 1)[0]) == 1


def test_rate_tpm_and_expired_slot_leases():
    from vam.gateway.limiter import LocalLimiter
    s = settings(model_rpm_workspace=3, model_tpm_provider=50, model_rpm_global=-1, model_rpm_provider=-1,
                 model_inflight_provider=-1, model_inflight_workspace=1)
    limiter = LocalLimiter(s)
    one = limiter.acquire(provider="p", workspace="a", lane="interactive", tokens=10, permit_id="x", lease_ms=1)
    import time
    time.sleep(0.01)
    # The holder never released: its slot lease expired, so the next call is admitted.
    two = limiter.acquire(provider="p", workspace="a", lane="interactive", tokens=10, permit_id="y", lease_ms=60_000)
    assert type(one).__name__ == type(two).__name__ == "Permit"
    limiter.release(two)
    third = limiter.acquire(provider="p", workspace="a", lane="interactive", tokens=40, permit_id="z", lease_ms=60_000)
    assert third.limit == "provider_tpm"
    limiter.release(limiter.acquire(provider="p", workspace="a", lane="interactive", tokens=10, permit_id="w", lease_ms=60_000))
    assert limiter.acquire(provider="p", workspace="a", lane="interactive", tokens=1, permit_id="v", lease_ms=1).limit == "workspace_rpm"


def test_local_fallback_divides_limits_by_the_instance_count():
    from vam.gateway.limiter import LocalLimiter
    s = settings(model_inflight_provider=8, model_inflight_workspace=-1, model_rpm_global=-1, model_rpm_provider=-1,
                 model_tpm_provider=-1, model_rpm_workspace=-1)
    assert len(acquire(LocalLimiter(s, share=4), 8)[0]) == 2


# ---------------------------------------------------------------------------- gateway
class DownLimiter:
    name = "redis"

    def acquire(self, **_):
        from vam.gateway.limiter import LimiterUnavailable
        raise LimiterUnavailable("ConnectionError")


def gateway(s, limiter=None, fallback=None):
    from vam.gateway import ModelGateway
    from vam.gateway.ledger import NullLedger
    from vam.gateway.limiter import LocalLimiter
    return ModelGateway(s, limiter or LocalLimiter(s), NullLedger(), fallback=fallback)


def test_provider_429_is_retried_with_backoff_and_4xx_is_not():
    from vam.gateway import ProviderError
    g = gateway(settings(model_retry_base_s=0, model_max_attempts=3))
    seen = []

    async def flaky():
        seen.append(1)
        if len(seen) < 3:
            raise ProviderError("provider_http_429", status=429, retryable=True, sent=False)
        return "ok", None
    assert asyncio.run(g.run(operation="chat", model="m", attempt_fn=flaky, est_input_tokens=5)) == "ok"
    assert len(seen) == 3 and g.snapshot()["retries"] == 2

    async def bad_request():
        seen.append(2)
        raise ProviderError("provider_http_400", status=400, retryable=False, sent=False)
    with pytest.raises(ProviderError):
        asyncio.run(g.run(operation="chat", model="m", attempt_fn=bad_request, est_input_tokens=5))
    assert seen.count(2) == 1


def test_attempt_timeout_is_an_unknown_outcome_and_bounded():
    from vam.gateway import ProviderError
    g = gateway(settings(model_max_attempts=1))

    async def hang():
        await asyncio.sleep(5)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(g.run(operation="chat", model="m", attempt_fn=hang, est_input_tokens=5, timeout=0.05))
    assert exc.value.code == "provider_timeout" and g.snapshot()["outcomes"] == {"unknown": 1}


def test_shared_limiter_down_rejects_by_default_and_never_calls_the_provider():
    from vam.video_registry import DomainError
    called = []

    async def call():
        called.append(1)
        return "ok", None
    with pytest.raises(DomainError) as exc:
        asyncio.run(gateway(settings(), DownLimiter()).run(operation="chat", model="m", attempt_fn=call, est_input_tokens=5))
    assert exc.value.status == 503 and called == []


def test_shared_limiter_down_with_explicit_local_policy_uses_the_divided_local_limit():
    from vam.gateway.limiter import LocalLimiter
    s = settings(model_limiter_on_redis_error="local", api_instance_count=2, model_inflight_provider=-1,
                 model_inflight_workspace=-1, model_rpm_global=-1, model_rpm_provider=-1, model_tpm_provider=-1,
                 model_rpm_workspace=4, model_queue_wait_s=0)
    g = gateway(s, DownLimiter(), fallback=LocalLimiter(s, share=s.api_instance_count))

    async def call():
        return "ok", None

    async def burst():
        return await asyncio.gather(*(g.run(operation="chat", model="m", attempt_fn=call, est_input_tokens=1)
                                      for _ in range(4)), return_exceptions=True)
    results = asyncio.run(burst())
    assert results.count("ok") == 2 and g.snapshot()["limiter_errors"] == 4


def test_interactive_waiters_are_bounded_and_rejected_with_429():
    from vam.video_registry import DomainError
    s = settings(model_inflight_provider=1, model_inflight_workspace=-1, model_rpm_global=-1, model_rpm_provider=-1,
                 model_tpm_provider=-1, model_rpm_workspace=-1, model_queue_wait_s=0.2, model_max_waiters=1)
    g = gateway(s)
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return "ok", None

    async def scenario():
        first = asyncio.create_task(g.run(operation="chat", model="m", attempt_fn=slow, est_input_tokens=1))
        await asyncio.sleep(0.05)
        waiting = asyncio.create_task(g.run(operation="chat", model="m", attempt_fn=slow, est_input_tokens=1))
        await asyncio.sleep(0.02)
        with pytest.raises(DomainError) as over:     # a second waiter exceeds MODEL_MAX_WAITERS at once
            await g.run(operation="chat", model="m", attempt_fn=slow, est_input_tokens=1)
        assert over.value.status == 429
        with pytest.raises(DomainError) as late:     # the waiter gives up after MODEL_QUEUE_WAIT_S
            await waiting
        assert late.value.code == "model_rate_limited"
        gate.set()
        assert await first == "ok"
    asyncio.run(scenario())


# ---------------------------------------------------------------------------- cache and events
def test_local_cache_coalesces_identical_work_and_never_caches_failures():
    from vam.cache import LocalCache
    cache = LocalCache(settings())
    computed = []

    async def compute():
        computed.append(1)
        await asyncio.sleep(0.05)
        return [1.0, 2.0]

    async def scenario():
        values = await asyncio.gather(*(cache.get_or_compute("k", compute, ttl=60) for _ in range(10)))
        assert all(v == [1.0, 2.0] for v in values) and len(computed) == 1
        assert cache.stats.coalesced == 9

        async def fail():
            raise RuntimeError("provider down")
        with pytest.raises(RuntimeError):
            await cache.get_or_compute("bad", fail, ttl=60)
        assert await cache.get_or_compute("bad", compute, ttl=60) == [1.0, 2.0]   # retried, not a cached error
    asyncio.run(scenario())


def test_cache_keys_separate_scope_model_and_text():
    from vam.cache import cache_key
    base = cache_key("qvec", "ws_a", {"model": "m1"}, "search_query", "red")
    assert base != cache_key("qvec", "ws_b", {"model": "m1"}, "search_query", "red")
    assert base != cache_key("qvec", "ws_a", {"model": "m2"}, "search_query", "red")
    assert base != cache_key("qvec", "ws_a", {"model": "m1"}, "search_query", "blue")


def test_local_events_drop_oldest_progress_and_end_on_terminal_event():
    from vam.events import LocalEventBus
    bus = LocalEventBus()
    queue = asyncio.Queue(maxsize=2)
    bus.subscribe(("c", "r"), queue)
    for i in range(3):
        bus.publish(("c", "r"), {"type": "progress", "i": i})
    assert [queue.get_nowait()["i"] for _ in range(2)] == [1, 2]
    bus.publish(("c", "r"), {"type": "final"})
    assert bus.subscribers == {}
