"""E3 on REAL Redis: shared model limits, slot leases, single-flight cache with a late (stale) executor,
cross-instance notifications, Redis unreachable. The last test runs two API instances in multi-instance
mode on real PostgreSQL + S3 + Redis: a question executing on instance A is followed live from a socket
connected to instance B. Model calls are a controllable simulator (no paid model).

Required: EVIREEL_TEST_REDIS_URL (redis://host:port/db; the test uses a key prefix it deletes). The
two-instance test additionally needs the E1 PostgreSQL/S3 variables. Skipped (and reported) otherwise;
CI runs it in .github/workflows/e3-concurrency.yml.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from unittest.mock import patch

import pytest

from vam.config import Settings

REDIS_URL = os.environ.get("EVIREEL_TEST_REDIS_URL", "")
pytestmark = pytest.mark.skipif(not REDIS_URL, reason="real Redis not configured (EVIREEL_TEST_REDIS_URL)")


def settings(**overrides):
    base = dict(_env_file=None, redis_url=REDIS_URL, model_rpm_global=-1, model_rpm_provider=-1, model_tpm_provider=-1,
                model_rpm_workspace=-1, model_inflight_provider=-1, model_inflight_workspace=-1, model_inflight_offline=-1)
    return Settings(**(base | overrides))


@pytest.fixture
def redis():
    import redis as redis_lib
    client = redis_lib.Redis.from_url(REDIS_URL, socket_timeout=2)
    client.flushdb()
    yield client
    client.flushdb()


def limiter_pair(s):
    import redis as redis_lib
    from vam.gateway.limiter import RedisLimiter
    # Two clients = two processes' views (separate connections); one shared budget in Redis.
    return [RedisLimiter(redis_lib.Redis.from_url(REDIS_URL, socket_timeout=2), s) for _ in range(2)]


def take(limiter, *, lane="interactive", workspace="ws", tokens=1, lease_ms=60_000):
    return limiter.acquire(provider="p", workspace=workspace, lane=lane, tokens=tokens,
                           permit_id=uuid.uuid4().hex, lease_ms=lease_ms)


def test_instances_share_one_global_rate_and_tpm_budget(redis):
    from vam.gateway.limiter import Permit
    pair = limiter_pair(settings(model_rpm_global=5, model_tpm_provider=400))
    admitted = [isinstance(take(pair[i % 2]), Permit) for i in range(10)]
    assert sum(admitted) == 5                                        # not 5 per instance
    pair = limiter_pair(settings(model_tpm_provider=100))
    redis.flushdb()
    assert isinstance(take(pair[0], tokens=60), Permit)
    denied = take(pair[1], tokens=60)
    assert denied.limit == "provider_tpm"


def test_slot_lease_of_a_dead_holder_expires_and_offline_keeps_a_reserve(redis):
    from vam.gateway.limiter import Permit
    a, b = limiter_pair(settings(model_inflight_provider=3, model_inflight_offline=2))
    held = take(a, lane="offline", lease_ms=300)                       # holder "dies": never released
    assert isinstance(take(b, lane="offline"), Permit)
    assert take(a, lane="offline").limit == "offline_inflight"         # offline cap 2 < provider cap 3
    assert isinstance(take(b, lane="interactive"), Permit)            # the reserve stays for questions
    time.sleep(0.4)
    assert isinstance(take(a, lane="offline"), Permit)                 # the dead holder's lease expired
    assert held.permit_id


def test_gateway_429_retry_on_shared_limits(redis):
    from vam.gateway import ModelGateway, ProviderError
    from vam.gateway.ledger import NullLedger
    s = settings(model_retry_base_s=0, model_rpm_workspace=3, model_queue_wait_s=0)
    gateway = ModelGateway(s, limiter_pair(s)[0], NullLedger())
    attempts = []

    async def provider():
        attempts.append(1)
        if len(attempts) == 1:
            raise ProviderError("provider_http_429", status=429, retryable=True, sent=False, retry_after=0)
        return "ok", {"input_tokens": 1, "output_tokens": 1}
    assert asyncio.run(gateway.run(operation="chat", model="sim", attempt_fn=provider, est_input_tokens=1)) == "ok"
    # Both attempts were charged to the shared window: one call left out of 3.
    from vam.video_registry import DomainError
    assert asyncio.run(gateway.run(operation="chat", model="sim", attempt_fn=provider, est_input_tokens=1)) == "ok"
    with pytest.raises(DomainError) as exc:
        asyncio.run(gateway.run(operation="chat", model="sim", attempt_fn=provider, est_input_tokens=1))
    assert exc.value.status == 429


def test_unreachable_redis_rejects_model_calls_instead_of_running_unlimited():
    import redis as redis_lib
    from vam.gateway import build_gateway
    from vam.video_registry import DomainError
    dead = redis_lib.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
    gateway = build_gateway(settings(), redis_client=dead)
    called = []

    async def provider():
        called.append(1)
        return "ok", None
    with pytest.raises(DomainError) as exc:
        asyncio.run(gateway.run(operation="chat", model="sim", attempt_fn=provider, est_input_tokens=1))
    assert exc.value.status == 503 and called == []
    # Explicit opt-in: a per-process limiter with every limit divided by the instance count.
    local = build_gateway(settings(model_limiter_on_redis_error="local", api_instance_count=2, model_rpm_workspace=4,
                                   model_queue_wait_s=0), redis_client=dead)
    results = [asyncio.run(local.run(operation="chat", model="sim", attempt_fn=provider, est_input_tokens=1))
               if i < 2 else None for i in range(2)]
    assert results == ["ok", "ok"]
    with pytest.raises(DomainError):
        asyncio.run(local.run(operation="chat", model="sim", attempt_fn=provider, est_input_tokens=1))


def test_cache_single_flight_across_instances_and_a_stale_executor_cannot_overwrite(redis):
    import redis as redis_lib
    from vam.cache import RedisCache
    s = settings(cache_lock_s=0.3)
    a, b = (RedisCache(redis_lib.Redis.from_url(REDIS_URL, socket_timeout=2), s) for _ in range(2))
    computed = []

    async def compute(value, delay):
        computed.append(value)
        await asyncio.sleep(delay)
        return value

    async def coalesced():
        return await asyncio.gather(a.get_or_compute("k1", lambda: compute("v", .1), ttl=60),
                                    b.get_or_compute("k1", lambda: compute("v", .1), ttl=60))
    assert asyncio.run(coalesced()) == ["v", "v"] and computed == ["v"]

    async def stale():
        # A holds the lock longer than cache_lock_s; B then computes and writes; A's late write is refused.
        slow = asyncio.create_task(a.get_or_compute("k2", lambda: compute("old", .8), ttl=60))
        await asyncio.sleep(.05)
        fresh = await b.get_or_compute("k2", lambda: compute("new", 0), ttl=60)
        return await slow, fresh
    old, new = asyncio.run(stale())
    assert (old, new) == ("old", "new")
    assert a.stats.stale_writes == 1
    assert asyncio.run(b.get_or_compute("k2", lambda: compute("other", 0), ttl=60)) == "new"


def test_events_reach_the_other_instance_once(redis):
    import redis as redis_lib
    from vam.events import RedisEventBus

    async def scenario():
        a = RedisEventBus(redis_lib.Redis.from_url(REDIS_URL), "api-a")
        b = RedisEventBus(redis_lib.Redis.from_url(REDIS_URL), "api-b")
        await a.start()
        await b.start()
        try:
            await asyncio.sleep(.3)
            on_a, on_b = asyncio.Queue(10), asyncio.Queue(10)
            a.subscribe(("c", "r"), on_a)
            b.subscribe(("c", "r"), on_b)
            a.publish(("c", "r"), {"type": "progress", "n": 1})
            a.publish(("c", "r"), {"type": "final"})
            got = [await asyncio.wait_for(on_b.get(), 5), await asyncio.wait_for(on_b.get(), 5)]
            assert [e["type"] for e in got] == ["progress", "final"]
            assert on_a.qsize() == 2                                    # delivered locally once, not echoed
        finally:
            await a.close()
            await b.close()
    asyncio.run(scenario())


# ---------------------------------------------------------------------------- two API instances, real everything
def test_question_on_instance_a_is_followed_from_a_socket_on_instance_b(tmp_path, monkeypatch, redis):
    from e1_support import REAL_INFRA
    if not REAL_INFRA:
        pytest.skip("real PostgreSQL + S3 not configured")
    from fastapi.testclient import TestClient
    import vam.config as config
    from e2_factory import Backend
    from e2_support import drain_inline, e2_values, make_media, upload_video
    from e1_support import scratch_bucket, scratch_database
    from test_e1_platform import users
    from test_e3_platform import login
    from vam import conversation_service as module
    from vam.pipeline.context import WorkerContext
    from vam.platform.db import PlatformDB
    from vam.server.app import create_app
    monkeypatch.setenv("E2_COUNTER_DIR", str(tmp_path))
    data = make_media(tmp_path / "red.mp4").read_bytes()
    with scratch_database() as db_url, scratch_bucket() as (bucket, _):
        PlatformDB(db_url).migrate()
        infra = {"db_url": db_url, "bucket": bucket}

        def make(name):
            s = Settings(**e2_values(tmp_path, infra, registry_backend="postgres", api_multi_instance=True,
                                     api_instance_count=2, redis_url=REDIS_URL, instance_id=f"api-{name}"))
            return s, create_app(settings=s, backend=Backend())
        sa, app_a = make("a")
        monkeypatch.setattr(config, "_settings", sa)
        _, app_b = make("b")
        gate = asyncio.Event()

        async def agent(**kwargs):
            yield {"type": "status", "stage": "retrieving"}
            for _ in range(200):          # the test releases the answer once B is subscribed
                if gate.is_set():
                    break
                await asyncio.sleep(.02)
            yield {"type": "final", "text": "answer from A", "result": {"evidence": [], "citations": []}}
        with patch.object(module, "stream_agent_response", agent), TestClient(app_a) as a, TestClient(app_b) as b:
            assert app_a.state.indexing.owner is None and app_b.state.indexing.owner is None
            alice_a, _ = users(a, app_a, "alice")["alice"]
            view = upload_video(a, alice_a, data)
            vid = view["video_id"]
            assert drain_inline(WorkerContext(sa), view["job_id"])["status"] == "succeeded"
            alice_b = login(b, "alice")
            cid = alice_a.post("/api/conversations", json={"video_id": vid}).json()["conversation_id"]
            rid = "r_" + uuid.uuid4().hex
            question = {"type": "text", "video_id": vid, "conversation_id": cid, "request_id": rid, "text": "red slide?"}
            with a.websocket_connect("/ws/agent", headers=alice_a.headers) as ws_a:
                ws_a.receive_json()
                ws_a.send_json(question)
                assert ws_a.receive_json()["type"] == "accepted"
                # Same request id on B: replayed (no second execution), subscribed to A's live events.
                with b.websocket_connect("/ws/agent", headers=alice_b.headers) as ws_b:
                    ws_b.receive_json()
                    ws_b.send_json(question)
                    accepted = ws_b.receive_json()
                    assert accepted["type"] == "accepted" and accepted["replayed"] is True
                    row = alice_b.get(f"/api/conversations/{cid}/requests/{rid}").json()
                    assert row["status"] in {"accepted", "running"}
                    gate.set()
                    events = []
                    while not events or events[-1]["type"] not in {"final", "error"}:
                        events.append(ws_b.receive_json())
                    assert events[-1]["type"] == "final" and "answer from A" in events[-1]["text"]
            assert app_b.state.events.received >= 1                      # it came over Redis Pub/Sub
            assert alice_b.get(f"/api/conversations/{cid}/requests/{rid}").json()["status"] == "completed"
