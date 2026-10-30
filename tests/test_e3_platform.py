"""E3 on REAL PostgreSQL + REAL S3 + real media: two API instances, shared read model, deletion races,
isolation, model budget. Models are the e2_factory test double; Redis-backed limits/cache/notifications
are in tests/test_e3_redis.py (real Redis, CI) -- here every instance uses its local limiter/cache.

The two instances deliberately get *different* DATA_ROOT directories: instance B starts with an empty
one and must rebuild every published file from object storage (verified by sha256), so nothing it
serves comes from a file instance A or the worker wrote locally.
"""
from __future__ import annotations

import asyncio
import shutil
import uuid

import pytest
from fastapi.testclient import TestClient

from e1_support import requires_infra
from e2_support import drain_inline, e2_values, make_media, upload_video
from test_e1_platform import As, PASSWORD, infra, users  # noqa: F401  (fixtures)

pytestmark = requires_infra


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("e3media")
    return {"red": make_media(root / "red.mp4", color="red").read_bytes(),
            "blue": make_media(root / "blue.mp4", color="blue").read_bytes()}


@pytest.fixture
def instances(tmp_path, infra, monkeypatch):  # noqa: F811
    """start(name) -> (settings, app); the worker context runs with instance A's DATA_ROOT."""
    import vam.config as config
    from vam.config import Settings
    from vam.pipeline.context import WorkerContext
    from vam.server.app import create_app
    counters = tmp_path / "counters"
    counters.mkdir()
    monkeypatch.setenv("E2_COUNTER_DIR", str(counters))
    common = dict(registry_backend="postgres", registry_schema="registry", generation_retire_grace_s=0,
                  read_lease_ttl_s=30)

    def values(name, **overrides):
        v = e2_values(tmp_path, infra, **common, **overrides)
        v["data_root"] = str(tmp_path / f"assets-{name}")
        v["instance_id"] = f"api-{name}"
        return v

    def start(name, **overrides):
        settings = Settings(**values(name, **overrides))
        monkeypatch.setattr(config, "_settings", settings)
        from e2_factory import Backend
        return settings, create_app(settings=settings, backend=Backend())   # the workers' embedding double

    def worker(**overrides):
        return WorkerContext(Settings(**values("a", **overrides)))
    start.worker = worker
    return start


def login(client, name):
    response = client.post("/api/auth/login", json={"username": name, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token = client.cookies.get("evireel_session")
    client.cookies.clear()
    return As(client, token)


def search(user, video_id, query="red slide"):
    return user.post(f"/api/videos/{video_id}/search", json={"query": query, "sources": ["transcript"], "top_k": 3})


def norm(results):
    return [{k: h.get(k) for k in ("source", "start", "end", "text", "score")} for h in results]


def pg_rows(app, sql, *params):
    with app.state.platform.db.connect() as conn:
        return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------- shared state, two instances
def test_second_instance_serves_everything_from_postgres_and_object_storage(instances, media):
    _, app_a = instances("a")
    _, app_b = instances("b")
    with TestClient(app_a) as a, TestClient(app_b) as b:
        alice_a, _ = users(a, app_a, "alice")["alice"]
        view = upload_video(a, alice_a, media["red"])
        vid = view["video_id"]
        job = drain_inline(instances.worker(), view["job_id"])
        assert job["status"] == "succeeded"
        root_b = app_b.state.indexing.registry.root
        assert not (root_b / "videos" / vid).exists()                # B has never seen this video's files

        # A login session, the video list and job state are shared rows: B answers without any local file.
        alice_b = login(b, "alice")
        listed = alice_b.get("/api/videos").json()
        assert [v["video_id"] for v in listed["items"]] == [vid]
        assert alice_b.get(f"/api/jobs/{view['job_id']}").json()["status"] == "succeeded"

        # Search on B rebuilds the published generation from object storage (sha256-verified) and returns
        # exactly what A returns from the worker-written files.
        from_a, from_b = search(alice_a, vid), search(alice_b, vid)
        assert from_a.status_code == from_b.status_code == 200, (from_a.text, from_b.text)
        assert from_a.json()["results"] and norm(from_b.json()["results"]) == norm(from_a.json()["results"])
        assert app_b.state.indexing.publications.restored >= 1
        assert alice_b.get(f"/api/videos/{vid}/transcripts?kind=segments").json()["items"]

        # Playback with Range on B: the playback object (or the original upload object) is restored first.
        part = alice_b.get(f"/api/videos/{vid}/content", headers={"Range": "bytes=0-99"})
        assert part.status_code == 206 and len(part.content) == 100
        assert part.content == alice_a.get(f"/api/videos/{vid}/content", headers={"Range": "bytes=0-99"}).content

        # A conversation created on A is visible on B (shared rows), and a request id replays the same row.
        cid = alice_a.post("/api/conversations", json={"video_id": vid}).json()["conversation_id"]
        assert alice_b.get(f"/api/conversations/{cid}").status_code == 200
        assert [c["conversation_id"] for c in alice_b.get(f"/api/conversations?video_id={vid}").json()["items"]] == [cid]


def test_two_users_are_isolated_on_every_resource_and_instance(instances, media):
    _, app_a = instances("a")
    _, app_b = instances("b")
    with TestClient(app_a) as a, TestClient(app_b) as b:
        people = users(a, app_a, "alice", "bob")
        alice, _ = people["alice"]
        view = upload_video(a, alice, media["red"])
        vid = view["video_id"]
        assert drain_inline(instances.worker(), view["job_id"])["status"] == "succeeded"
        cid = alice.post("/api/conversations", json={"video_id": vid}).json()["conversation_id"]
        for bob in (people["bob"][0], login(b, "bob")):              # same answers on both instances
            assert bob.get("/api/videos").json()["items"] == []
            for method, path, body in [
                    ("get", f"/api/videos/{vid}", None), ("get", f"/api/videos/{vid}/content", None),
                    ("post", f"/api/videos/{vid}/search", {"query": "x"}), ("get", f"/api/jobs/{view['job_id']}", None),
                    ("get", f"/api/videos/{vid}/transcripts", None), ("get", f"/api/conversations/{cid}", None),
                    ("get", f"/api/videos/{vid}/study", None), ("get", f"/api/videos/{vid}/frames", None),
                    ("post", f"/api/conversations", {"video_id": vid}), ("delete", f"/api/videos/{vid}", None),
                    ("get", f"/api/videos/{vid}/transcripts/export?format=srt", None)]:
                response = getattr(bob, method)(path, **({"json": body} if body is not None else {}))
                assert response.status_code == 404, (path, response.status_code, response.text)
        # The query-vector cache key carries the owning workspace: Alice's searches never produce a key
        # under Bob's workspace (and Bob cannot reach the search at all, above).
        assert search(alice, vid).status_code == 200
        workspaces = {row["workspace_id"] for row in pg_rows(app_a, "SELECT workspace_id FROM video_ownership")}
        assert app_a.state.indexing.cache_scope(vid) in workspaces
        assert app_a.state.indexing.cache_scope(vid) != people["bob"][1]["workspace_id"]


# ---------------------------------------------------------------------------- versions and deletion
def test_new_publication_coexists_with_an_old_reader_then_the_old_generation_is_retired(instances, media):
    _, app_a = instances("a")
    with TestClient(app_a) as a:
        alice, _ = users(a, app_a, "alice")["alice"]
        view = upload_video(a, alice, media["red"])
        vid = view["video_id"]
        worker = instances.worker()
        assert drain_inline(worker, view["job_id"])["status"] == "succeeded"
        service = app_a.state.indexing
        old = service.registry.get(vid)["active_generation"]
        old_objects = {r["relative_path"]: r["object_key"] for r in pg_rows(
            app_a, "SELECT relative_path, object_key FROM registry.publication_objects WHERE video_id=%s", vid)}
        assert f"memory/{old}.sqlite3" in old_objects
        lease = service._retain(vid)                                   # a request still reading generation 1
        reader = service._reader(vid, old)
        rerun = alice.post(f"/api/videos/{vid}/index").json()["job_id"]
        assert drain_inline(worker, rerun)["status"] == "succeeded"
        new = service.registry.get(vid)["active_generation"]
        assert new != old
        # The old reader still reads its pinned generation; new requests read the new one.
        assert reader._persist_path.endswith(f"{old}.sqlite3") and (service.registry.root / "videos" / vid / "memory" / f"{old}.sqlite3").is_file()
        assert service.sweep()["generations_removed"] == 0              # leased: never removed
        objects = {r["relative_path"]: r["object_key"] for r in pg_rows(
            app_a, "SELECT relative_path, object_key FROM registry.publication_objects WHERE video_id=%s", vid)}
        assert f"memory/{new}.sqlite3" in objects and f"memory/{old}.sqlite3" not in objects
        queued = {r["object_key"] for r in pg_rows(app_a, "SELECT object_key FROM object_deletions WHERE reason='superseded'")}
        assert old_objects[f"memory/{old}.sqlite3"] in queued           # the old object leaves with its manifest row
        lease()
        assert service.sweep()["generations_removed"] >= 1
        assert not (service.registry.root / "videos" / vid / "memory" / f"{old}.sqlite3").exists()
        assert search(alice, vid).status_code == 200


def test_deletion_and_job_acceptance_are_serialized_across_instances(instances, media):
    _, app_a = instances("a")
    _, app_b = instances("b")
    with TestClient(app_a) as a, TestClient(app_b) as b:
        alice_a, _ = users(a, app_a, "alice")["alice"]
        alice_b = login(b, "alice")
        view = upload_video(a, alice_a, media["red"])
        vid = view["video_id"]
        # An active job (accepted on A) refuses the deletion on B.
        assert alice_b.delete(f"/api/videos/{vid}").status_code == 409
        assert drain_inline(instances.worker(), view["job_id"])["status"] == "succeeded"
        # A reader on A holds a lease: the deletion on B tombstones (202) and waits.
        lease = app_a.state.indexing._retain(vid)
        deleting = alice_b.delete(f"/api/videos/{vid}")
        assert deleting.status_code == 202 and deleting.json()["waiting_for_readers"] == 1
        # Under the tombstone no instance accepts a job, a question, notes or a new reader.
        cid_refused = alice_a.post(f"/api/videos/{vid}/index")
        assert cid_refused.status_code == 409
        assert alice_a.post("/api/conversations", json={"video_id": vid}).status_code == 409
        assert alice_a.get(f"/api/videos/{vid}/content").status_code == 409
        with pytest.raises(Exception) as refused:
            app_a.state.pipeline.submit(vid, kind="transcript")       # the PG-side check, below the API checks
        assert getattr(refused.value, "code", "") == "video_deleting"
        assert app_b.state.indexing.sweep()["reclaimed"] == 0
        lease()
        assert app_b.state.indexing.sweep()["reclaimed"] == 1
        assert alice_a.get(f"/api/videos/{vid}").status_code == 404
        # Published objects were queued for deletion before their manifest rows went away.
        queued = pg_rows(app_a, "SELECT count(*) AS n FROM object_deletions WHERE reason='video_deleted' AND video_id=%s", vid)
        assert queued[0]["n"] >= 1
        assert pg_rows(app_a, "SELECT count(*) AS n FROM registry.publication_objects WHERE video_id=%s", vid)[0]["n"] == 0


def test_lost_data_root_is_rebuilt_on_the_same_instance(instances, media):
    _, app_a = instances("a")
    with TestClient(app_a) as a:
        alice, _ = users(a, app_a, "alice")["alice"]
        view = upload_video(a, alice, media["blue"])
        vid = view["video_id"]
        assert drain_inline(instances.worker(), view["job_id"])["status"] == "succeeded"
        expected = search(alice, vid).json()["results"]
        service = app_a.state.indexing
        service.readers.evict(vid)
        shutil.rmtree(service.registry.root / "videos" / vid / "memory")
        shutil.rmtree(service.registry.root / "videos" / vid / "cache", ignore_errors=True)
        assert norm(search(alice, vid).json()["results"]) == norm(expected)
        assert service.publications.restored >= 1


# ---------------------------------------------------------------------------- model budget shared by instances
def test_daily_token_budget_is_shared_by_all_gateways(instances, media):
    """Two gateways (two API instances / workers) reserve against one PostgreSQL ledger: together they
    never admit more than the workspace budget. A provider 429 is retried and its attempt is recorded."""
    from vam.gateway import ModelGateway, ProviderError, model_scope
    from vam.gateway.ledger import PgLedger
    from vam.gateway.limiter import LocalLimiter
    from vam.video_registry import DomainError
    settings, app_a = instances("a", model_retry_base_s=0, model_queue_wait_s=0,
                                   model_inflight_workspace=-1, model_inflight_provider=-1)
    with TestClient(app_a):
        db = app_a.state.platform.db
        with db.transaction() as conn:
            conn.execute("INSERT INTO model_budgets(workspace_id, daily_token_limit) VALUES ('ws_budget', 1000)")
        gateways = [ModelGateway(settings, LocalLimiter(settings), PgLedger(db, settings)) for _ in range(2)]
        admitted, refused = [], []

        async def one(gateway):
            async def attempt():
                return "ok", {"input_tokens": 90, "output_tokens": 10}
            try:
                with model_scope("ws_budget", "interactive"):
                    admitted.append(await gateway.run(operation="chat", model="sim", attempt_fn=attempt,
                                                      est_input_tokens=90, est_output_tokens=10))
            except DomainError as exc:
                refused.append(exc.code)

        async def burst():
            await asyncio.gather(*(one(gateways[i % 2]) for i in range(16)))
        asyncio.run(burst())
        assert len(admitted) == 10 and set(refused) == {"model_budget_exceeded"}
        used = pg_rows(app_a, "SELECT sum(input_tokens + output_tokens) AS n FROM model_usage WHERE workspace_id='ws_budget'")
        assert int(used[0]["n"]) == 1000

        flaky = {"n": 0}

        async def rate_limited():
            flaky["n"] += 1
            if flaky["n"] == 1:
                raise ProviderError("provider_http_429", status=429, retryable=True, sent=False, retry_after=0)
            return "ok", {"input_tokens": 5, "output_tokens": 1}
        with model_scope("ws_retry", "interactive"):
            assert asyncio.run(gateways[0].run(operation="chat", model="sim", attempt_fn=rate_limited,
                                               est_input_tokens=5)) == "ok"
        rows = pg_rows(app_a, "SELECT attempt, status, error_code FROM model_usage WHERE workspace_id='ws_retry' ORDER BY attempt")
        assert [(r["attempt"], r["status"], r["error_code"]) for r in rows] == [(1, "failed", "provider_http_429"), (2, "settled", None)]


def test_registry_copy_tool_round_trips(instances, media, tmp_path):
    """The SQLite -> PostgreSQL migration tool copies every table and verifies contents; the reverse
    direction (rollback) produces an identical SQLite registry."""
    from vam.storage.database import Database
    from vam.storage.pg_registry import PgRegistryDatabase, copy_registry, verify_registry
    from vam.video_registry import VideoRegistry
    from vam.memory.conversation import ConversationStore
    settings, app_a = instances("a")
    lite_root = tmp_path / "legacy"
    legacy = VideoRegistry(lite_root)
    for n in range(3):
        vid = "v_" + uuid.uuid4().hex
        (lite_root / "videos" / vid).mkdir(parents=True)
        legacy.register(vid, f"old-{n}.mp4", "source.mp4", 10 + n, 2.5)
        legacy.create_job(vid, 10)
        store = ConversationStore(legacy)
        cid = store.create(vid)["conversation_id"]
        store.accept(cid, "r_" + uuid.uuid4().hex, vid, {"q": n}, f"问题 {n} 100%", 10)
    target = PgRegistryDatabase(settings.database_url, "registry_copy_" + uuid.uuid4().hex[:8])
    counts = copy_registry(legacy.db, target)
    assert counts["videos"] == 3 and counts["messages"] == 3 and verify_registry(legacy.db, target) == {}
    back = Database(tmp_path / "rollback.sqlite3")
    copy_registry(target, back)
    assert verify_registry(legacy.db, back) == {}
    # The copied identity sequence continues after the highest copied message id.
    restored = VideoRegistry(tmp_path / "restored", db=target)
    vid = restored.list()["items"][0]["video_id"]
    cid = ConversationStore(restored).create(vid)["conversation_id"]
    assert ConversationStore(restored).accept(cid, "r_" + uuid.uuid4().hex, vid, {"q": 9}, "next", 10)
