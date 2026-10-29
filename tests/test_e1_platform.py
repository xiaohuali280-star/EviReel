"""E1 acceptance: resumable direct uploads on REAL PostgreSQL + REAL S3-compatible storage + real media.

Model calls are replaced by the deterministic P1 colour backend (no paid API); everything
on the upload path (database, object storage, ffprobe/ffmpeg, indexing pipeline) is real.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from e1_support import (MiB, PASSWORD, S3, fingerprint, make_video, md5_b64, part_bytes, put_part, requires_infra,
                        scratch_bucket, scratch_database, sign, upload_parts, wait_job, wait_state)

pytestmark = requires_infra


class As:
    """Act as one user via a Bearer token on a shared TestClient (cookies cleared)."""
    def __init__(self, client, token):
        self.client, self.headers = client, {"Authorization": f"Bearer {token}"}

    def __getattr__(self, method):
        def call(url, **kwargs):
            kwargs["headers"] = {**self.headers, **kwargs.get("headers", {})}
            return getattr(self.client, method)(url, **kwargs)
        return call


@pytest.fixture(scope="module")
def video_file(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("e1media") / "lecture.mp4", 11 * MiB)


@pytest.fixture
def infra():
    with scratch_database() as db_url, scratch_bucket() as (bucket, s3):
        from vam.platform.db import PlatformDB
        PlatformDB(db_url).migrate()
        yield {"db_url": db_url, "bucket": bucket, "s3": s3}


def make_settings(tmp_path, infra, **overrides):
    from vam.config import Settings
    values = dict(_env_file=None, data_root=str(tmp_path / "assets"), frame_store_path=str(tmp_path / "default.sqlite3"),
                  enable_retrieval=False, asr_enabled=False, video_fps=1, video_laplacian_min=0, video_diff_threshold=0,
                  video_ssim_threshold=1.1, enable_event_finalize=False, query_include_latest_segment_video=False,
                  study_auto_generate=False, siliconflow_api_key="", dashscope_api_key="", openrouter_api_key="",
                  database_url=infra["db_url"], s3_endpoint=S3["endpoint"], s3_public_endpoint=S3["endpoint"],
                  s3_access_key=S3["access_key"], s3_secret_key=S3["secret_key"], s3_bucket=infra["bucket"],
                  upload_part_size=5 * MiB, upload_reconcile_interval_s=3600, upload_lease_s=30)
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def platform_app(tmp_path, infra, monkeypatch):
    import vam.config as config
    from test_p1_video_management import ColorBackend
    from vam.server.app import create_app

    def start(object_store=None, **overrides):
        settings = make_settings(tmp_path, infra, **overrides)
        monkeypatch.setattr(config, "_settings", settings)
        return settings, create_app(settings=settings, backend=ColorBackend(), object_store=object_store)
    return start


class DeleteOutage:
    """The REAL S3 store, with DeleteObject failing while ``down`` (injected storage fault)."""
    def __init__(self, infra):
        from vam.platform.object_store import S3ObjectStore
        self.real = S3ObjectStore(endpoint=S3["endpoint"], public_endpoint=S3["endpoint"], access_key=S3["access_key"],
                                  secret_key=S3["secret_key"], bucket=infra["bucket"])
        self.down, self.failed_deletes = True, 0

    def delete(self, key):
        if self.down:
            from vam.platform.object_store import ObjectStoreError
            self.failed_deletes += 1
            raise ObjectStoreError("injected: storage delete unavailable")
        return self.real.delete(key)

    def __getattr__(self, name):
        return getattr(self.real, name)


def users(client, app, *names, quota=None):
    from vam.platform.identity import IdentityService
    identity = IdentityService(app.state.platform.db, app.state.indexing.settings)
    out = {}
    for name in names:
        created = identity.create_user(name, PASSWORD, quota_bytes=quota)
        response = client.post("/api/auth/login", json={"username": name, "password": PASSWORD})
        assert response.status_code == 200, response.text
        out[name] = (As(client, client.cookies.get("evireel_session")), created)
        client.cookies.clear()
    return out


def create(user, data, name="lecture.mp4"):
    response = user.post("/api/uploads", json={"filename": name, "size": len(data), "fingerprint": fingerprint(data),
                                               "content_type": "video/mp4"})
    assert response.status_code == 201, response.text
    return response.json()


def sqlite_rows(app, sql, *params):
    with app.state.indexing.registry.db.connect() as conn:
        return conn.execute(sql, params).fetchall()


def pg(app, sql, *params):
    with app.state.platform.db.connect() as conn:
        return conn.execute(sql, params).fetchall()


# 1 + 6: interrupt, "refresh", resend only missing parts; bytes identical; playback/Range/index work.
def test_resume_sends_only_missing_parts_and_new_video_is_playable(platform_app, video_file):
    settings, app = platform_app()
    data = video_file.read_bytes()
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data)
        assert session["part_count"] >= 3 and session["state"] == "uploading"
        upload_parts(alice, session, data, [1, 3])                        # connection drops before part 2
        resumed = alice.get(f"/api/uploads/{session['upload_id']}").json()  # page refresh: server-side truth
        assert resumed["uploaded_parts"] == [1, 3] and resumed["missing_parts"] == [2] + list(range(4, session["part_count"] + 1))
        # The resuming client checks the reselected file against these (storage-confirmed) digests.
        assert resumed["uploaded_part_md5"] == {str(n): md5_b64(part_bytes(data, session, n)) for n in (1, 3)}
        edited = bytearray(data); edited[(session["part_size"] // 2)] ^= 0xFF
        assert md5_b64(part_bytes(bytes(edited), session, 1)) != resumed["uploaded_part_md5"]["1"]
        assert alice.get("/api/uploads").json()["items"][0]["upload_id"] == session["upload_id"]
        upload_parts(alice, session, data, resumed["missing_parts"])
        accepted = alice.post(f"/api/uploads/{session['upload_id']}/complete")
        assert accepted.status_code in (200, 202) and accepted.json()["state"] in ("verifying", "completed")
        done = wait_state(alice, session["upload_id"])
        assert done["state"] == "completed", done
        stored = app.state.platform.store
        key = pg(app, "SELECT object_key FROM upload_sessions WHERE upload_id=%s", session["upload_id"])[0]["object_key"]
        assert hashlib.sha256(b"".join(stored.iter_object(key))).hexdigest() == hashlib.sha256(data).hexdigest()
        assert done["result"]["sha256"] == hashlib.sha256(data).hexdigest()
        video_id = session["video_id"]
        job_id = wait_state(alice, session["upload_id"])["job_id"] or alice.get(f"/api/videos/{video_id}").json()["latest_job_id"]
        assert job_id
        assert wait_job(alice, job_id)["status"] in {"succeeded", "partial"}
        detail = alice.get(f"/api/videos/{video_id}").json()
        assert detail["capabilities"]["playback"] is True
        ranged = alice.get(f"/api/videos/{video_id}/content", headers={"Range": "bytes=0-99"})
        assert ranged.status_code == 206 and len(ranged.content) == 100
        assert [v["video_id"] for v in alice.get("/api/videos").json()["items"]] == [video_id]
        ws = pg(app, "SELECT used_bytes, reserved_bytes FROM workspaces")[0]
        assert (ws["used_bytes"], ws["reserved_bytes"]) == (len(data), 0)


# 2: part reached storage but its progress registration never arrived.
def test_progress_reconciles_with_storage_when_registration_is_lost(platform_app, video_file):
    _, app = platform_app()
    data = video_file.read_bytes()
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data)
        urls = sign(alice, session["upload_id"], session, data, [2])
        assert put_part(urls[2], part_bytes(data, session, 2)).status_code == 200   # no /done call
        assert pg(app, "SELECT state FROM upload_parts WHERE part_number=2")[0]["state"] == "signed"
        view = alice.get(f"/api/uploads/{session['upload_id']}").json()
        assert view["uploaded_parts"] == [2]
        assert pg(app, "SELECT state FROM upload_parts WHERE part_number=2")[0]["state"] == "uploaded"


# 3: duplicate/concurrent complete -> one logical video, one processing acceptance, same answer on retry.
def test_concurrent_and_repeated_complete_create_one_video_and_one_job(platform_app, video_file):
    _, app = platform_app()
    data = video_file.read_bytes()
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data)
        upload_parts(alice, session, data, list(range(1, session["part_count"] + 1)))
        with ThreadPoolExecutor(6) as pool:
            responses = list(pool.map(lambda _: alice.post(f"/api/uploads/{session['upload_id']}/complete"), range(6)))
        assert all(r.status_code in (200, 202) for r in responses), [r.text for r in responses]
        assert {r.json()["video_id"] for r in responses} == {session["video_id"]}
        done = wait_state(alice, session["upload_id"])
        assert done["state"] == "completed"
        retry = alice.post(f"/api/uploads/{session['upload_id']}/complete")      # client lost the first response
        assert retry.status_code == 200 and retry.json()["result"] == done["result"]
        assert len(sqlite_rows(app, "SELECT 1 FROM videos")) == 1
        deadline = time.monotonic() + 30
        while not pg(app, "SELECT job_id FROM upload_sessions")[0]["job_id"] and time.monotonic() < deadline:
            time.sleep(0.2)
        assert len(sqlite_rows(app, "SELECT 1 FROM index_jobs WHERE video_id=?", session["video_id"])) == 1
        assert len(pg(app, "SELECT 1 FROM video_ownership")) == 1
        ws = pg(app, "SELECT used_bytes, reserved_bytes FROM workspaces")[0]
        assert (ws["used_bytes"], ws["reserved_bytes"]) == (len(data), 0)


# 4a: object assembled, database transition failed -> reconcile resumes without re-uploading.
def test_merge_succeeds_but_db_write_fails_then_reconcile_recovers(platform_app, video_file):
    _, app = platform_app()
    data = video_file.read_bytes()
    with TestClient(app, raise_server_exceptions=False) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data)
        upload_parts(alice, session, data, list(range(1, session["part_count"] + 1)))
        uploads = app.state.platform.uploads
        original = uploads._mark_verifying
        def crash(upload_id):
            raise RuntimeError("injected: database unavailable after CompleteMultipartUpload")
        uploads._mark_verifying = crash
        assert alice.post(f"/api/uploads/{session['upload_id']}/complete").status_code == 500
        uploads._mark_verifying = original
        row = pg(app, "SELECT state, object_key FROM upload_sessions")[0]
        assert row["state"] == "completing"
        assert app.state.platform.store.head(row["object_key"]).size == len(data)       # merged in storage
        assert alice.post(f"/api/uploads/{session['upload_id']}/complete").json()["state"] == "completing"  # no double merge
        with app.state.platform.db.transaction() as conn:
            conn.execute("UPDATE upload_sessions SET lease_until = now() - interval '1 second'")
        summary = client.portal.call(uploads.reconcile)
        assert summary["merged"] == 1
        assert wait_state(alice, session["upload_id"])["state"] == "completed"
        assert len(sqlite_rows(app, "SELECT 1 FROM videos")) == 1


# 4b: verification crashed mid-way (lease expires) -> another attempt publishes once.
def test_interrupted_verification_is_retried_once_lease_expires(platform_app, video_file):
    _, app = platform_app()
    data = video_file.read_bytes()
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data)
        upload_parts(alice, session, data, list(range(1, session["part_count"] + 1)))
        uploads = app.state.platform.uploads
        uploads.start_verification = lambda upload_id: None          # process "dies" before verifying
        assert alice.post(f"/api/uploads/{session['upload_id']}/complete").json()["state"] == "verifying"
        del uploads.start_verification
        summary = client.portal.call(uploads.reconcile)
        assert summary["verify_restarted"] == 1
        assert wait_state(alice, session["upload_id"])["state"] == "completed"
        assert pg(app, "SELECT verify_attempts FROM upload_sessions")[0]["verify_attempts"] == 1


# 4c: corrupted part, wrong file, expired signature.
def test_storage_rejects_corrupted_part_and_signed_digest_mismatch(platform_app, video_file):
    _, app = platform_app()
    data = video_file.read_bytes()
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data)
        urls = sign(alice, session["upload_id"], session, data, [1])
        good = part_bytes(data, session, 1)
        corrupted = bytes([good[0] ^ 0xFF]) + good[1:]
        rejected = put_part(urls[1], corrupted, md5_b64(good))        # bytes changed in transit
        assert rejected.status_code == 400 and "BadDigest" in rejected.text
        tampered = put_part(urls[1], corrupted, md5_b64(corrupted))  # header not covered by our signature
        assert tampered.status_code == 403
        assert alice.get(f"/api/uploads/{session['upload_id']}").json()["uploaded_parts"] == []
        missing = alice.post(f"/api/uploads/{session['upload_id']}/complete")
        assert missing.status_code == 409 and missing.json()["error"]["code"] == "parts_missing"
        assert alice.get(f"/api/uploads/{session['upload_id']}").json()["state"] == "uploading"


def test_non_video_content_fails_verification_and_releases_everything(platform_app):
    _, app = platform_app()
    data = b"not a video " * (1 << 19)  # ~6 MiB, two parts, .mp4 name
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data, "renamed.mp4")
        upload_parts(alice, session, data, list(range(1, session["part_count"] + 1)))
        alice.post(f"/api/uploads/{session['upload_id']}/complete")
        failed = wait_state(alice, session["upload_id"])
        assert failed["state"] == "failed" and failed["error"]["code"] == "invalid_media"
        assert sqlite_rows(app, "SELECT 1 FROM videos") == []
        assert pg(app, "SELECT 1 FROM video_ownership") == []
        assert alice.get(f"/api/videos/{session['video_id']}").status_code == 404
        ws = pg(app, "SELECT used_bytes, reserved_bytes FROM workspaces")[0]
        assert (ws["used_bytes"], ws["reserved_bytes"]) == (0, 0)
        key = pg(app, "SELECT object_key FROM upload_sessions")[0]["object_key"]
        from vam.platform.object_store import NoSuchObject
        with pytest.raises(NoSuchObject):
            app.state.platform.store.head(key)


def test_expired_signature_is_rejected_and_resigning_works(platform_app, video_file):
    # X-Amz-Date is truncated to whole seconds, so a URL is valid for (ttl-1, ttl] seconds; a 1 s TTL
    # left the re-signed URL as little as 0 s and made the second PUT flaky.
    _, app = platform_app(upload_url_ttl_s=3)
    data = video_file.read_bytes()
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data)
        urls = sign(alice, session["upload_id"], session, data, [1])
        time.sleep(4.5)
        expired = put_part(urls[1], part_bytes(data, session, 1))
        assert expired.status_code == 403
        fresh = sign(alice, session["upload_id"], session, data, [1])
        assert put_part(fresh[1], part_bytes(data, session, 1)).status_code == 200


# 5: cross-user isolation, identity from server session only.
def test_cross_user_access_is_denied_everywhere(platform_app, video_file):
    _, app = platform_app()
    data = video_file.read_bytes()
    with TestClient(app) as client:
        made = users(client, app, "alice", "mallory")
        alice, mallory = made["alice"][0], made["mallory"][0]
        session = create(alice, data)
        upload_parts(alice, session, data, list(range(1, session["part_count"] + 1)))
        alice.post(f"/api/uploads/{session['upload_id']}/complete")
        done = wait_state(alice, session["upload_id"])
        video_id = done["video_id"]
        conversation = alice.post("/api/conversations", json={"video_id": video_id})
        uid = session["upload_id"]
        for method, url, body in [
            ("get", f"/api/uploads/{uid}", None), ("post", f"/api/uploads/{uid}/complete", None),
            ("delete", f"/api/uploads/{uid}", None),
            ("post", f"/api/uploads/{uid}/parts/sign", {"parts": [{"part_number": 1, "size": 5 * MiB, "md5_b64": "A" * 22 + "=="}]}),
            ("get", f"/api/videos/{video_id}", None), ("get", f"/api/videos/{video_id}/content", None),
            ("get", f"/api/videos/{video_id}/transcripts", None), ("get", f"/api/videos/{video_id}/study", None),
            ("post", f"/api/videos/{video_id}/trash", None), ("delete", f"/api/videos/{video_id}", None),
            ("post", f"/api/videos/{video_id}/index", None), ("post", "/api/conversations", {"video_id": video_id}),
            ("get", f"/api/conversations?video_id={video_id}", None),
        ] + ([("get", f"/api/conversations/{conversation.json()['conversation_id']}", None)] if conversation.status_code == 201 else []) + (
            [("get", f"/api/jobs/{done['job_id']}", None)] if done["job_id"] else []):
            response = getattr(mallory, method)(url, **({"json": body} if body is not None else {}))
            assert response.status_code == 404, (method, url, response.status_code, response.text)
        assert mallory.get("/api/videos").json()["items"] == []
        assert mallory.get("/api/uploads").json()["items"] == []
        # Identity cannot be smuggled through the request body.
        smuggled = mallory.post("/api/uploads", json={"filename": "x.mp4", "size": 10, "fingerprint": "f" * 16,
                                                      "workspace_id": "ws_" + "0" * 32, "user_id": "usr_" + "0" * 32})
        assert smuggled.status_code == 422
        # Unauthenticated and forged credentials.
        assert client.get("/api/videos").status_code == 401
        assert client.get("/api/videos", headers={"Authorization": "Bearer forged"}).status_code == 401
        assert client.post("/api/videos", files={"video": ("a.mp4", b"x", "video/mp4")},
                           headers=mallory.headers).status_code == 410
        with client.websocket_connect("/ws/agent", headers=mallory.headers) as socket:
            socket.receive_json()
            socket.send_json({"type": "text", "video_id": video_id, "conversation_id": "c_" + "1" * 32,
                              "request_id": "r_" + "1" * 32, "text": "hi"})
            assert socket.receive_json()["code"] == "video_not_found"
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/agent") as socket:
                socket.receive_json()


# 5: quota cannot be bypassed by concurrent sessions.
def test_concurrent_sessions_cannot_exceed_quota(platform_app):
    _, app = platform_app()
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice", quota=25 * MiB)["alice"]
        def attempt(i):
            return alice.post("/api/uploads", json={"filename": f"{i}.mp4", "size": 10 * MiB, "fingerprint": f"fp-{i:08d}"})
        with ThreadPoolExecutor(5) as pool:
            codes = sorted(r.status_code for r in pool.map(attempt, range(5)))
        assert codes == [201, 201, 413, 413, 413]
        ws = pg(app, "SELECT reserved_bytes FROM workspaces")[0]
        assert ws["reserved_bytes"] == 20 * MiB
        first = alice.get("/api/uploads").json()["items"][0]
        alice.delete(f"/api/uploads/{first['upload_id']}")
        assert pg(app, "SELECT reserved_bytes FROM workspaces")[0]["reserved_bytes"] == 10 * MiB
        assert attempt(9).status_code == 201


# 5: cancel and complete race - the database decides, invariants hold either way.
def test_cancel_and_complete_race_has_one_winner(platform_app, video_file):
    _, app = platform_app()
    data = video_file.read_bytes()
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        outcomes = []
        for _ in range(3):
            session = create(alice, data)
            upload_parts(alice, session, data, list(range(1, session["part_count"] + 1)))
            barrier = threading.Barrier(2)
            def do(action):
                barrier.wait()
                return action()
            with ThreadPoolExecutor(2) as pool:
                cancel = pool.submit(do, lambda: alice.delete(f"/api/uploads/{session['upload_id']}"))
                complete = pool.submit(do, lambda: alice.post(f"/api/uploads/{session['upload_id']}/complete"))
                c, m = cancel.result(), complete.result()
            final = wait_state(alice, session["upload_id"], ("completed", "failed", "aborted"))
            if final["state"] == "aborted":
                assert c.status_code == 200 and m.status_code == 409
            else:
                assert c.status_code == 409 and m.status_code in (200, 202) and final["state"] == "completed"
            outcomes.append(final["state"])
        from vam.platform.migrate import check
        report = check(app.state.platform.db)
        assert report["ok"], report
        completed = outcomes.count("completed")
        assert len(pg(app, "SELECT 1 FROM video_ownership")) == completed == len(sqlite_rows(app, "SELECT 1 FROM videos"))


# 5: expiry cleanup vs. late completion.
def test_expired_session_is_cleaned_and_cannot_complete(platform_app, video_file):
    _, app = platform_app()
    data = video_file.read_bytes()
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data)
        upload_parts(alice, session, data, [1])
        with app.state.platform.db.transaction() as conn:
            conn.execute("UPDATE upload_sessions SET expires_at = now() - interval '1 second'")
        late = alice.post(f"/api/uploads/{session['upload_id']}/complete")
        assert late.status_code == 410 and late.json()["error"]["code"] == "upload_expired"
        assert pg(app, "SELECT state, quota_state FROM upload_sessions")[0] == {"state": "expired", "quota_state": "released"}
        second = create(alice, data)
        upload_parts(alice, second, data, [1])
        with app.state.platform.db.transaction() as conn:
            conn.execute("UPDATE upload_sessions SET expires_at = now() - interval '1 second' WHERE upload_id=%s", (second["upload_id"],))
        summary = client.portal.call(app.state.platform.uploads.reconcile)
        assert summary["expired"] == 1
        assert app.state.platform.store.list_multipart_uploads("workspaces/") == []
        assert pg(app, "SELECT reserved_bytes FROM workspaces")[0]["reserved_bytes"] == 0


# 6 + migration: legacy data copy is backfilled, visible only to its owner, and rolled back.
def test_legacy_data_copy_migrates_and_rolls_back(tmp_path, infra, monkeypatch, video_file):
    import vam.config as config
    from test_p1_video_management import ColorBackend
    from vam.platform.migrate import backfill_legacy, check, rollback_legacy
    from vam.server.app import create_app

    legacy = make_settings(tmp_path, infra, database_url="", data_root=str(tmp_path / "legacy"))
    monkeypatch.setattr(config, "_settings", legacy)
    small = tmp_path / "small.mp4"
    shutil.copy(video_file, small)
    with TestClient(create_app(settings=legacy, backend=ColorBackend())) as single:
        created = single.post("/api/videos", files={"video": ("old.mp4", small.read_bytes(), "video/mp4")}).json()
        wait_job(single, created["job_id"])
        legacy_id = created["video_id"]
    original_registry = (tmp_path / "legacy" / "registry.sqlite3").read_bytes()
    copy_root = tmp_path / "legacy-copy"
    shutil.copytree(tmp_path / "legacy", copy_root)

    settings = make_settings(tmp_path, infra, data_root=str(copy_root))
    monkeypatch.setattr(config, "_settings", settings)
    app = create_app(settings=settings, backend=ColorBackend())
    with TestClient(app) as client:
        made = users(client, app, "alice", "bob")
        alice, alice_meta = made["alice"]
        bob = made["bob"][0]
        assert alice.get(f"/api/videos/{legacy_id}").status_code == 404           # invisible until migrated
        db = app.state.platform.db
        dry = backfill_legacy(db, str(copy_root), alice_meta["workspace_id"], dry_run=True)
        assert dry["to_assign"] == 1 and pg(app, "SELECT 1 FROM video_ownership") == []
        done = backfill_legacy(db, str(copy_root), alice_meta["workspace_id"], dry_run=False)
        assert done["inserted"] == 1
        assert backfill_legacy(db, str(copy_root), alice_meta["workspace_id"], dry_run=False)["to_assign"] == 0  # idempotent
        assert check(db, str(copy_root))["ok"]
        ranged = alice.get(f"/api/videos/{legacy_id}/content", headers={"Range": "bytes=10-19"})
        assert ranged.status_code == 206 and len(ranged.content) == 10
        assert alice.get(f"/api/videos/{legacy_id}").json()["status"] in {"succeeded", "partial"}
        assert bob.get(f"/api/videos/{legacy_id}").status_code == 404
        rolled = rollback_legacy(db, alice_meta["workspace_id"], dry_run=False)
        assert rolled["deleted"] == 1 and alice.get(f"/api/videos/{legacy_id}").status_code == 404
        assert pg(app, "SELECT used_bytes FROM workspaces WHERE workspace_id=%s", alice_meta["workspace_id"])[0]["used_bytes"] == 0
    assert (tmp_path / "legacy" / "registry.sqlite3").read_bytes() == original_registry      # source never touched
    with sqlite3.connect(copy_root / "registry.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1               # rollback keeps data


def test_deleting_an_uploaded_video_releases_quota_and_object(platform_app, video_file):
    _, app = platform_app()
    data = video_file.read_bytes()
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data)
        upload_parts(alice, session, data, list(range(1, session["part_count"] + 1)))
        alice.post(f"/api/uploads/{session['upload_id']}/complete")
        done = wait_state(alice, session["upload_id"])
        deadline = time.monotonic() + 30
        while not pg(app, "SELECT job_id FROM upload_sessions")[0]["job_id"] and time.monotonic() < deadline:
            time.sleep(0.2)
        wait_job(alice, pg(app, "SELECT job_id FROM upload_sessions")[0]["job_id"])
        assert alice.delete(f"/api/videos/{done['video_id']}").status_code == 204
        assert pg(app, "SELECT used_bytes FROM workspaces")[0]["used_bytes"] == 0
        assert pg(app, "SELECT 1 FROM video_ownership") == []
        from vam.platform.object_store import NoSuchObject
        with pytest.raises(NoSuchObject):
            app.state.platform.store.head(pg(app, "SELECT object_key FROM upload_sessions")[0]["object_key"])


def _object_exists(store, key):
    from vam.platform.object_store import NoSuchObject
    try:
        store.head(key)
        return True
    except NoSuchObject:
        return False


def _drain_after_backoff(app):
    # First retry is due 5 s after the failed attempt; wait it out instead of rewriting timestamps.
    time.sleep(5.5)
    return asyncio.run(app.state.platform.uploads.reconcile())["objects_deleted"]


# Storage DELETE fails, the process restarts, storage recovers -> the object is eventually removed.
def test_video_delete_during_storage_outage_converges_after_restart(platform_app, infra, video_file):
    outage = DeleteOutage(infra)
    _, app = platform_app(object_store=outage)
    data = video_file.read_bytes()
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data)
        upload_parts(alice, session, data, list(range(1, session["part_count"] + 1)))
        alice.post(f"/api/uploads/{session['upload_id']}/complete")
        done = wait_state(alice, session["upload_id"])
        assert done["state"] == "completed"
        deadline = time.monotonic() + 30
        while not pg(app, "SELECT job_id FROM upload_sessions")[0]["job_id"] and time.monotonic() < deadline:
            time.sleep(0.2)
        wait_job(alice, pg(app, "SELECT job_id FROM upload_sessions")[0]["job_id"])
        key = pg(app, "SELECT object_key FROM upload_sessions")[0]["object_key"]
        assert alice.delete(f"/api/videos/{done['video_id']}").status_code == 204
        # Logical deletion is complete: no ownership, quota released, video gone for the user.
        assert pg(app, "SELECT 1 FROM video_ownership") == []
        assert pg(app, "SELECT used_bytes FROM workspaces")[0]["used_bytes"] == 0
        assert alice.get(f"/api/videos/{done['video_id']}").status_code == 404
        # Physical deletion failed and is recorded durably, not just logged.
        assert outage.failed_deletes == 1 and _object_exists(outage.real, key)
        row = pg(app, "SELECT reason, state, attempts, last_error FROM object_deletions WHERE object_key=%s", key)[0]
        assert (row["reason"], row["state"], row["attempts"]) == ("video_deleted", "pending", 1)
        assert "injected" in row["last_error"]
    # "Restart" with storage still down: the pending row survives and is retried, still pending.
    _, app = platform_app(object_store=outage)
    with TestClient(app):
        assert _drain_after_backoff(app) == 0
        assert pg(app, "SELECT state, attempts FROM object_deletions")[0] == {"state": "pending", "attempts": 2}
    # Storage recovers; the next process converges.
    outage.down = False
    _, app = platform_app(object_store=outage)
    with TestClient(app):
        deadline = time.monotonic() + 30  # second backoff step is 10 s; startup or a later pass may do it
        while pg(app, "SELECT state FROM object_deletions")[0]["state"] == "pending" and time.monotonic() < deadline:
            asyncio.run(app.state.platform.uploads.reconcile())
            time.sleep(1)
        assert not _object_exists(outage.real, key)
        row = pg(app, "SELECT state, done_at FROM object_deletions WHERE object_key=%s", key)[0]
        assert row["state"] == "done" and row["done_at"] is not None
        assert asyncio.run(app.state.platform.uploads.reconcile())["objects_deleted"] == 0  # idempotent


def test_failed_verification_object_is_cleaned_after_storage_recovers(platform_app, infra):
    outage = DeleteOutage(infra)
    _, app = platform_app(object_store=outage)
    data = b"not a video " * (1 << 19)
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data, "renamed.mp4")
        upload_parts(alice, session, data, list(range(1, session["part_count"] + 1)))
        alice.post(f"/api/uploads/{session['upload_id']}/complete")
        failed = wait_state(alice, session["upload_id"])
        assert failed["state"] == "failed" and failed["error"]["code"] == "invalid_media"
        key = pg(app, "SELECT object_key FROM upload_sessions")[0]["object_key"]
        ws = pg(app, "SELECT used_bytes, reserved_bytes FROM workspaces")[0]
        assert (ws["used_bytes"], ws["reserved_bytes"]) == (0, 0)
        assert _object_exists(outage.real, key)
        assert pg(app, "SELECT reason, state FROM object_deletions")[0] == {"reason": "upload_failed", "state": "pending"}
        outage.down = False
        assert _drain_after_backoff(app) == 1
        assert not _object_exists(outage.real, key)
        assert pg(app, "SELECT state FROM object_deletions")[0]["state"] == "done"


def test_cleanup_never_deletes_an_object_that_is_still_owned(platform_app, video_file):
    _, app = platform_app()
    data = video_file.read_bytes()
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        session = create(alice, data)
        upload_parts(alice, session, data, list(range(1, session["part_count"] + 1)))
        alice.post(f"/api/uploads/{session['upload_id']}/complete")
        assert wait_state(alice, session["upload_id"])["state"] == "completed"
        key = pg(app, "SELECT object_key FROM upload_sessions")[0]["object_key"]
        from vam.platform.cleanup import enqueue
        with app.state.platform.db.transaction() as conn:   # e.g. a stale or mistaken queue entry
            enqueue(conn, key, "video_deleted")
        assert app.state.platform.uploads.cleaner.delete_now(key) is False
        assert _object_exists(app.state.platform.store, key)
        assert pg(app, "SELECT state, last_error FROM object_deletions")[0] == {"state": "skipped", "last_error": "still_referenced"}


# Idempotent create: a lost or cancelled-in-flight POST can be settled by replaying its request id.
def test_create_is_idempotent_per_request_id_and_replay_finds_cancelled_session(platform_app, video_file):
    _, app = platform_app()
    data = video_file.read_bytes()
    body = {"filename": "lecture.mp4", "size": len(data), "fingerprint": fingerprint(data), "content_type": "video/mp4",
            "client_request_id": "cr-" + "a" * 32}
    with TestClient(app) as client:
        alice, _ = users(client, app, "alice")["alice"]
        with ThreadPoolExecutor(4) as pool:   # concurrent replays of one request: one session, one reservation
            responses = list(pool.map(lambda _: alice.post("/api/uploads", json=body), range(4)))
        assert {r.status_code for r in responses} == {201}
        assert len({r.json()["upload_id"] for r in responses}) == 1
        upload_id = responses[0].json()["upload_id"]
        assert pg(app, "SELECT COUNT(*) AS n FROM upload_sessions")[0]["n"] == 1
        assert pg(app, "SELECT reserved_bytes FROM workspaces")[0]["reserved_bytes"] == len(data)
        assert alice.delete(f"/api/uploads/{upload_id}").json()["state"] == "aborted"
        replay = alice.post("/api/uploads", json=body)     # after the cancel: reports it, creates nothing
        assert replay.json()["upload_id"] == upload_id and replay.json()["state"] == "aborted"
        assert pg(app, "SELECT COUNT(*) AS n FROM upload_sessions")[0]["n"] == 1
        assert pg(app, "SELECT reserved_bytes FROM workspaces")[0]["reserved_bytes"] == 0
        fresh = alice.post("/api/uploads", json=body | {"client_request_id": "cr-" + "b" * 32})
        assert fresh.status_code == 201 and fresh.json()["upload_id"] != upload_id
        assert alice.post("/api/uploads", json=body | {"client_request_id": "bad id!"}).status_code == 422
        # Request ids are scoped to the workspace: bob reusing alice's id gets his own session.
        bob, _ = users(client, app, "bob")["bob"]
        mine = bob.post("/api/uploads", json=body)
        assert mine.status_code == 201 and mine.json()["upload_id"] != upload_id
