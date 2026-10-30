"""P1 contracts: real media/storage, deterministic model mocks, no paid API."""
import asyncio
import base64
import io
import json
import sqlite3
import subprocess
import time
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from PIL import Image
from fastapi.testclient import TestClient

import vam.config as config
from vam.config import Settings
from vam.server.app import create_app
from vam.indexing.service import IndexingService
from vam.retrieval.frame_store import FrameStore
from vam.vision.backends import OpenRouterGeminiBackend, validate_embeddings

class ColorBackend:
    name = "test-color-v1"
    fail = False

    def embed_image_base64(self, value, *, instruction):
        if self.fail:
            raise RuntimeError("mock embedding outage")
        pixels = np.asarray(Image.open(io.BytesIO(base64.b64decode(value.split(",", 1)[1]))).convert("RGB"))
        vector = pixels.mean(axis=(0, 1)).astype(float)
        vector = vector / max(float(np.linalg.norm(vector)), 1)
        return vector.tolist(), 3

    def embed_images_base64_batch(self, images, *, instruction, batch_size):
        return [self.embed_image_base64(v, instruction=instruction)[0] for v in images], 3

    def embed_texts_batch(self, texts, *, instruction, batch_size):
        return [([0., 0., 1.] if "blue" in t.lower() else [1., 0., 0.]) for t in texts], 3

    def embed_text(self, text, *, instruction):
        return self.embed_texts_batch([text], instruction=instruction, batch_size=1)[0][0], 3

@pytest.fixture(scope="session")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    for color in ("red", "blue"):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"color=c={color}:s=160x120:r=4", "-t", "2", "-c:v", "mpeg4", "-y",
                        str(root / f"{color}.mp4")], check=True, timeout=30)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=red:s=160x120:r=4", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-y", str(root / "h264.mp4")], check=True, timeout=30)
    return root

@pytest.fixture
def settings(tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, data_root=str(tmp_path / "assets"), enable_retrieval=False,
                   frame_store_path=str(tmp_path / "default.sqlite3"), video_fps=1,
                   video_laplacian_min=0, video_diff_threshold=0, video_ssim_threshold=1.1,
                   query_include_latest_segment_video=False, enable_event_finalize=True,
                   siliconflow_api_key="", dashscope_api_key="", openrouter_api_key="")
    monkeypatch.setattr(config, "_settings", cfg)
    return cfg

@pytest.fixture
def backend():
    return ColorBackend()

@pytest.fixture
def client(settings, backend):
    with patch("vam.llm.openrouter.OpenRouterClient.chat", new_callable=AsyncMock, return_value=("A visible red or blue slide.", {})):
        with TestClient(create_app(settings=settings, backend=backend)) as http:
            yield http

def terminal(client, job_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(.025)
    raise AssertionError("job did not reach terminal state")

def upload(client, media, color="red", filename="same.mp4"):
    result = client.post("/api/videos", files={"video": (filename, (media / f"{color}.mp4").read_bytes(), "video/mp4")})
    assert result.status_code == 202, result.text
    body = result.json()
    job = terminal(client, body["job_id"])
    assert job["status"] == "succeeded", job
    return body["video_id"], body["job_id"]

def test_upload_index_and_original_persistence(client, media):
    vid, jid = upload(client, media)
    detail = client.get(f"/api/videos/{vid}").json()
    assert detail["capabilities"] == {"playback": True, "vision": True, "transcript": False, "transcript_search": False}
    row = client.app.state.indexing.registry.get(vid)
    assert client.app.state.indexing.registry.asset(vid, row["source_path"]).read_bytes() == (media / "red.mp4").read_bytes()
    assert client.get("/api/videos").json()["total"] == 1
    job = client.get(f"/api/jobs/{jid}").json()
    assert job["attempt"] == 1 and job["result"]["count"] > 0
    assert job["result"]["model"]["dimension"] == 3
    assert "source_path" not in detail and "model_fingerprint" not in detail

def test_same_names_and_frame_ids_do_not_mix(client, media):
    a, _ = upload(client, media, "red")
    b, _ = upload(client, media, "blue")
    af = client.get(f"/api/videos/{a}/frames").json()
    bf = client.get(f"/api/videos/{b}/frames").json()
    assert [f["frame_id"] for f in af["items"]] == [f["frame_id"] for f in bf["items"]]
    for vid in (a, b):
        hits = client.post(f"/api/videos/{vid}/search", json={"query": "red"}).json()["results"]
        assert hits and all(h["video_id"] == vid for h in hits)
        docs = client.get(f"/api/videos/{vid}/memory-documents").json()
        assert docs["total"] > 0 and all(d["video_id"] == vid for d in docs["items"])
    image_a = client.get(af["items"][0]["image_url"]).content
    image_b = client.get(bf["items"][0]["image_url"]).content
    assert image_a != image_b
    assert client.get(f"/api/videos/{a}").json()["active_generation"] != client.get(f"/api/videos/{b}").json()["active_generation"]

def test_range_and_content_type(client, media):
    vid, _ = upload(client, media)
    url = f"/api/videos/{vid}/content"
    whole = client.get(url)
    assert whole.status_code == 200 and whole.headers["content-type"].startswith("video/mp4")
    partial = client.get(url, headers={"Range": "bytes=0-31"})
    assert partial.status_code == 206 and partial.content == whole.content[:32]
    assert partial.headers["content-range"].startswith("bytes 0-31/")
    tail = client.get(url, headers={"Range": "bytes=-10"})
    assert tail.status_code == 206 and tail.content == whole.content[-10:]
    assert client.get(url, headers={"Range": "bytes=999999999-"}).status_code == 416
    assert not client.app.state.indexing.leases

def test_h264_playback_reuses_original(client, media):
    vid, _ = upload(client, media, "h264")
    assert client.app.state.indexing.registry.get(vid)["playback_path"] == "source.mp4"

def test_failed_rebuild_keeps_previous_index_and_retry(client, media, backend):
    vid, _ = upload(client, media)
    first = client.get(f"/api/videos/{vid}/frames").json()
    backend.fail = True
    jid = client.post(f"/api/videos/{vid}/index").json()["job_id"]
    failed = terminal(client, jid)
    assert failed["status"] == "failed" and failed["error_code"] == "index_failed"
    detail = client.get(f"/api/videos/{vid}").json()
    assert detail["active_generation"] == first["index_version"]
    assert detail["capabilities"]["vision"] is True
    assert client.get(f"/api/videos/{vid}/frames").json()["items"] == first["items"]
    assert not client.app.state.indexing.registry.asset(vid, f"memory/{jid}.sqlite3").exists()
    backend.fail = False
    jid2 = client.post(f"/api/videos/{vid}/index").json()["job_id"]
    assert terminal(client, jid2)["status"] == "succeeded"
    assert client.get(f"/api/jobs/{jid2}").json()["attempt"] == 3
    old_image = first["items"][0]["image_url"]
    assert client.get(old_image).status_code == 409

def test_delete_only_target(client, media):
    a, aj = upload(client, media)
    b, _ = upload(client, media, "blue")
    directory = client.app.state.indexing.registry.directory(a)
    assert client.delete(f"/api/videos/{a}").status_code == 204
    assert not directory.exists()
    assert client.get(f"/api/videos/{a}").status_code == 404
    assert client.get(f"/api/jobs/{aj}").status_code == 404
    assert client.get(f"/api/videos/{b}/content").status_code == 200
    assert client.get(f"/api/videos/{b}/frames").json()["total"] > 0

def test_delete_while_reading_tombstones_then_reclaims(client, media):
    """E3 deletion protocol: the tombstone blocks new readers and jobs at once; the reader already
    streaming keeps its file; reclamation happens after it releases (sweeper or a repeated DELETE)."""
    vid, _ = upload(client, media)
    service = client.app.state.indexing
    path, release = service.playback_asset(vid)
    try:
        response = client.delete(f"/api/videos/{vid}")
        assert response.status_code == 202 and response.json()["status"] == "deleting"
        assert response.json()["waiting_for_readers"] == 1
        assert path.is_file()                                            # the existing reader is not cut off
        assert client.post(f"/api/videos/{vid}/index").status_code == 409   # no new job
        assert client.get(f"/api/videos/{vid}/content").status_code == 409  # no new reader
        assert service.sweep()["reclaimed"] == 0                         # still leased
    finally:
        release()
    assert service.sweep()["reclaimed"] == 1
    assert not path.exists()
    assert client.get(f"/api/videos/{vid}").status_code == 404
    assert client.delete(f"/api/videos/{vid}").status_code == 404

def test_delete_failure_is_retryable(client, media, monkeypatch):
    vid, _ = upload(client, media)
    with patch("vam.indexing.service.shutil.rmtree", side_effect=OSError("busy file")):
        assert client.delete(f"/api/videos/{vid}").status_code == 500
    assert client.get(f"/api/videos/{vid}").json()["status"] == "delete_failed"
    assert client.delete(f"/api/videos/{vid}").status_code == 204

@pytest.mark.parametrize("filename,content,mime,expected", [
    ("movie.txt", b"not video", "text/plain", 415),
    ("movie.mp4", b"not video", "video/mp4", 415),
    ("movie.mp4", b"", "video/mp4", 422),
    ("movie.mp4", b"not video", "text/plain", 415),
])
def test_invalid_upload_cleanup(client, filename, content, mime, expected):
    result = client.post("/api/videos", files={"video": (filename, content, mime)})
    assert result.status_code == expected
    assert client.get("/api/videos").json()["total"] == 0
    assert list((client.app.state.indexing.registry.root / ".uploads").iterdir()) == []
    assert list((client.app.state.indexing.registry.root / "videos").iterdir()) == []

def test_filename_traversal_does_not_choose_storage(client, media):
    vid, _ = upload(client, media, filename="../../outside.mp4")
    row = client.app.state.indexing.registry.get(vid)
    assert row["source_path"] == "source.mp4" and row["original_name"] == "outside.mp4"

def test_request_body_and_file_limits(client, media, settings):
    settings.max_upload_bytes = 100
    # Exact file limit remains enforced even with multipart overhead below middleware limit.
    response = client.post("/api/videos", files={"video": ("a.mp4", (media / "red.mp4").read_bytes(), "video/mp4")})
    assert response.status_code == 413
    huge = client.post("/api/videos", content=b"x",
                       headers={"content-length": str(1024 * 1024 * 1024 + 65537)})
    assert huge.status_code == 413

def test_chunked_body_limit(settings, backend):
    settings.max_upload_bytes = 100
    with TestClient(create_app(settings=settings, backend=backend)) as client:
        def chunks():
            yield b"--bound\r\nContent-Disposition: form-data; name=\"video\"; filename=\"x.mp4\"\r\nContent-Type: video/mp4\r\n\r\n"
            yield b"x" * 70000
            yield b"\r\n--bound--\r\n"
        response = client.post("/api/videos", content=chunks(), headers={"content-type": "multipart/form-data; boundary=bound"})
        assert response.status_code == 413

def test_duration_and_storage_errors(client, media, settings):
    settings.max_video_duration_s = .1
    assert client.post("/api/videos", files={"video": ("a.mp4", (media / "red.mp4").read_bytes(), "video/mp4")}).status_code == 413
    settings.max_video_duration_s = 100
    with patch("vam.indexing.service.inspect_upload", side_effect=OSError(28, "full")):
        response = client.post("/api/videos", files={"video": ("a.mp4", (media / "red.mp4").read_bytes(), "video/mp4")})
        assert response.status_code == 507

def test_audio_only_upload_is_rejected(client, tmp_path):
    path = tmp_path / "audio.m4v"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=1000:duration=1", "-c:a", "aac", "-f", "mp4", "-y", str(path)], check=True)
    assert client.post("/api/videos", files={"video": ("a.mp4", path.read_bytes(), "video/mp4")}).status_code == 415

def test_disabled_path_endpoints_and_resource_validation(client):
    assert client.post("/api/frames/index_video_async_by_path", json={"video_path": "C:/Windows/private"}).status_code == 404
    assert client.post("/api/frames/clear", params={"store_path": "any.sqlite3"}).status_code == 404
    assert client.get("/api/videos/not-a-valid-id").status_code == 404
    assert client.get("/api/jobs/missing").status_code == 404
    assert client.get("/api/videos", params={"limit": 201}).status_code == 422

def test_tampered_asset_path_never_served(client, media):
    vid, _ = upload(client, media)
    service = client.app.state.indexing
    service.registry.update_video(vid, playback_path="../../registry.sqlite3")
    assert client.get(f"/api/videos/{vid}/content").status_code == 400

@pytest.mark.parametrize("body", [
    {"query": "red", "min_time": 10, "max_time": 5},
    {"query": " "},
    {"query": "red", "top_k": 0},
    {"query": "red", "store_path": "other.sqlite3"},
])
def test_search_rejects_bad_parameters(client, media, body):
    vid, _ = upload(client, media)
    assert client.post(f"/api/videos/{vid}/search", json=body).status_code == 422

def test_missing_and_not_ready_memory(client, media):
    service = client.app.state.indexing
    vid = "v_" + "a" * 32
    directory = service.registry.directory(vid)
    directory.mkdir()
    (directory / "source.mp4").write_bytes((media / "red.mp4").read_bytes())
    service.registry.register(vid, "x.mp4", "source.mp4", 1, 2)
    assert client.get(f"/api/videos/{vid}/frames").status_code == 409
    assert client.get(f"/api/videos/{vid}/content").status_code == 409
    assert client.post(f"/api/videos/{vid}/search", json={"query": "red"}).status_code == 409

@pytest.mark.parametrize("value", ["nan", "inf"])
def test_memory_time_range_must_be_finite(client, media, value):
    vid, _ = upload(client, media)
    assert client.get(f"/api/videos/{vid}/frames", params={"max_time": value}).status_code == 422

def test_model_change_requires_rebuild(client, media, backend):
    vid, _ = upload(client, media)
    backend.name = "different-model-same-dimension"
    assert client.post(f"/api/videos/{vid}/search", json={"query": "red"}).status_code == 409
    jid = client.post(f"/api/videos/{vid}/index").json()["job_id"]
    assert terminal(client, jid)["status"] == "succeeded"
    assert client.post(f"/api/videos/{vid}/search", json={"query": "red"}).status_code == 200

def test_legacy_identity_requires_explicit_rebuild(client, media):
    vid, _ = upload(client, media)
    service = client.app.state.indexing
    row = service.registry.get(vid)
    path = service.registry.asset(vid, f"memory/{row['active_generation']}.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE store_meta SET model_fingerprint=NULL")
        conn.execute("PRAGMA user_version=1")
    assert client.get(f"/api/videos/{vid}/frames").status_code == 409
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

@pytest.mark.parametrize("vectors,count,dim", [
    ([[1., 2.]], 2, 2),
    ([[1.], [1., 2.]], 2, None),
    ([[float("nan")]], 1, 1),
    ([[float("inf")]], 1, 1),
    ([[1e100]], 1, 1),
    ([[]], 1, None),
])
def test_bad_embedding_responses(vectors, count, dim):
    with pytest.raises(RuntimeError):
        validate_embeddings(vectors, expected_count=count, expected_dim=dim)

def test_single_text_adapter_returns_vector(settings):
    with patch("vam.llm.openrouter.OpenRouterEmbeddingsClient._post", return_value={"data": [{"embedding": [1., 0., 0.]}]}):
        vec, dim = OpenRouterGeminiBackend().embed_text("question", instruction="query")
    assert vec == [1., 0., 0.] and dim == 3

def test_embedding_indices_are_reordered_and_validated(settings):
    from vam.llm.openrouter import OpenRouterEmbeddingsClient
    adapter = OpenRouterEmbeddingsClient()
    vectors, _ = adapter._extract_embeddings({"data": [
        {"index": 1, "embedding": [0., 1.]}, {"index": 0, "embedding": [1., 0.]}]})
    assert vectors == [[1., 0.], [0., 1.]]
    with pytest.raises(RuntimeError, match="indices"):
        adapter._extract_embeddings({"data": [
            {"index": 0, "embedding": [1.]}, {"index": 0, "embedding": [0.]}]})

def test_fingerprint_does_not_persist_credentials(settings):
    settings.embedding_base_url_override = "https://user:secret@example.com/v1?api_key=secret"
    fingerprint = OpenRouterGeminiBackend().fingerprint()
    assert "secret" not in json.dumps(fingerprint) and "user:" not in fingerprint["endpoint"]

def test_anchor_frame_fallback_uses_nearby_video_frames():
    from types import SimpleNamespace
    from vam.retrieval.agent_search import _frames_for_anchor_result
    frame = SimpleNamespace(frame_id="f", t=10., absolute_t=None)
    store = SimpleNamespace(get_frame_by_id=AsyncMock(return_value=frame),
                            list_frames_in_time_range=AsyncMock(return_value=[]))
    frames = asyncio.run(_frames_for_anchor_result(store=store, result={"source":"frame","frame_id":"f"}, time_mode="relative"))
    assert frames == [frame]
    assert store.list_frames_in_time_range.await_args.kwargs["min_time"] == 8.

def test_no_usable_frames_is_failed(client, media, settings):
    settings.video_laplacian_min = 10**9
    result = client.post("/api/videos", files={"video": ("a.mp4", (media / "red.mp4").read_bytes(), "video/mp4")}).json()
    job = terminal(client, result["job_id"])
    assert job["status"] == "failed"
    assert client.get(f"/api/videos/{result['video_id']}").json()["capabilities"] == {
        "playback": True, "vision": False, "transcript": False, "transcript_search": False}

def test_tier_only_changes_are_persisted(settings, backend, tmp_path):
    store = FrameStore(backend=backend, persist_path=str(tmp_path / "tier.sqlite3"))
    def uri(color):
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), color).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    asyncio.run(store.add_frames(ts=[0., 10., 20.], images_base64=[uri(c) for c in ("red","green","blue")], frame_ids=["a","b","c"]))
    with sqlite3.connect(store._persist_path) as conn:
        conn.execute("UPDATE frames SET memory_tier='mid'")
    asyncio.run(store.apply_retention_policy())
    assert {f.memory_tier for f in asyncio.run(FrameStore(backend=backend,persist_path=store._persist_path).list_frames())} == {"recent"}

def test_finished_jobs_and_assets_survive_app_restart(settings, backend, media):
    with patch("vam.llm.openrouter.OpenRouterClient.chat", new_callable=AsyncMock, return_value=("red slide", {})):
        with TestClient(create_app(settings=settings, backend=backend)) as first:
            vid, jid = upload(first, media)
        with TestClient(create_app(settings=settings, backend=backend)) as second:
            assert second.get(f"/api/jobs/{jid}").json()["status"] == "succeeded"
            assert second.get(f"/api/videos/{vid}/content").status_code == 200
            assert second.get(f"/api/videos/{vid}/frames").json()["total"] > 0

def test_startup_interrupts_persisted_unfinished_job(settings, backend):
    service = IndexingService(settings, backend=backend)
    vid = "v_" + "b" * 32
    service.registry.directory(vid).mkdir()
    service.registry.register(vid, "x.mp4", "source.mp4", 1, 1)
    jid = service.registry.create_job(vid, 10)
    service.registry.update_job(jid, status="running")
    with TestClient(create_app(settings=settings, backend=backend)) as client:
        assert client.get(f"/api/jobs/{jid}").json()["status"] == "interrupted"

def test_second_worker_rejected(settings, backend):
    first = IndexingService(settings, backend=backend)
    second = IndexingService(settings, backend=backend)
    asyncio.run(first.start())
    try:
        with pytest.raises(RuntimeError, match="one worker"):
            asyncio.run(second.start())
    finally:
        asyncio.run(first.close())

def test_busy_index_and_shutdown_recovery(settings, backend, media):
    async def blocked(**kwargs):
        await asyncio.sleep(3600)
    with TestClient(create_app(settings=settings, backend=backend, indexer=blocked)) as client:
        result = client.post("/api/videos", files={"video": ("x.mp4", (media / "red.mp4").read_bytes(), "video/mp4")}).json()
        vid, jid = result["video_id"], result["job_id"]
        assert client.post(f"/api/videos/{vid}/index").status_code == 409
        assert client.delete(f"/api/videos/{vid}").status_code == 409
    with TestClient(create_app(settings=settings, backend=backend)) as client:
        assert client.get(f"/api/jobs/{jid}").json()["status"] == "interrupted"

def test_queue_full_retains_uploaded_video(settings, backend, media):
    settings.max_queued_jobs = 1
    async def blocked(**kwargs):
        await asyncio.sleep(3600)
    with TestClient(create_app(settings=settings, backend=backend, indexer=blocked)) as client:
        client.post("/api/videos", files={"video": ("a.mp4", (media / "red.mp4").read_bytes(), "video/mp4")}).json()
        two = client.post("/api/videos", files={"video": ("a.mp4", (media / "blue.mp4").read_bytes(), "video/mp4")}).json()
        assert two["job_id"] is None and two["index_error"]["code"] == "queue_full"
        assert client.get(f"/api/videos/{two['video_id']}").status_code == 200
        assert client.post(f"/api/videos/{two['video_id']}/index").status_code == 429

def test_partial_index_is_not_reported_full_success(client, media, settings):
    settings.video_max_frames = 1
    response = client.post("/api/videos", files={"video": ("a.mp4", (media / "red.mp4").read_bytes(), "video/mp4")}).json()
    assert terminal(client, response["job_id"])["status"] == "partial"
    assert client.get(f"/api/videos/{response['video_id']}").json()["capabilities"]["vision"] is True

def test_ws_video_binding_and_history_switch(client, media):
    from vam import agent
    from vam.protocol import RetrieveToolCall, RetrieveToolArgs
    a, _ = upload(client, media)
    b, _ = upload(client, media, "blue")
    observed = []
    async def retrieval(*, semantic, store_instance, chat_history, retrieval_context=None):
        frames = await store_instance.list_frames()
        observed.append((store_instance._persist_path, len(json.loads(chat_history[0]['content'])['turns'])))
        yield {"type": "final", "result": {"answer": "mock answer", "found": True,
                 "best": {"frame_id": frames[0].frame_id, "t": frames[0].t}, "turns": []}}
    decision = RetrieveToolCall(type="tool", name="retrieve", args=RetrieveToolArgs(question="red"))
    with patch.object(agent, "route_text_request", AsyncMock(return_value=decision)), patch.object(agent, "retrieval_search_generator", retrieval):
        with client.websocket_connect("/ws/agent") as ws:
            ws.receive_json()
            ws.send_json({"type":"text","text":"missing video"})
            assert ws.receive_json()["code"] == "invalid_request"
            for vid in (a, b, b):
                ws.send_json({"type":"text","video_id":vid,"text":"where?"})
                events = []
                while not events or events[-1]["type"] != "final":
                    events.append(ws.receive_json())
                    assert events[-1]["type"] != "error", events[-1]
                assert all(e["video_id"] == vid for e in events)
                assert events[-1]["result"]["best"]["video_id"] == vid
        with client.websocket_connect("/ws/agent") as ws:
            ws.receive_json()
            ws.send_json({"type":"text","video_id":b,"text":"reconnect"})
            while True:
                event = ws.receive_json()
                assert event["type"] != "error", event
                if event["type"] == "final":
                    break
    assert [n for _, n in observed] == [0,0,1,0]
    assert a in observed[0][0] and all(b in p for p, _ in observed[1:])

def test_ws_summary_writes_only_bound_video(client, media):
    a, _ = upload(client, media)
    b, _ = upload(client, media, "blue")
    with client.websocket_connect("/ws/agent") as ws:
        ws.receive_json()
        ws.send_json({"type":"tool","video_id":a,"name":"summarize",
                      "args":{"min_time":0,"max_time":2,"time_mode":"relative","granularity_seconds":30,"prompt":"summary"}})
        while True:
            event = ws.receive_json()
            if event["type"] in {"final","error"}:
                assert event["type"] == "final", event
                assert event["result"]["created"] > 0
                break
    assert client.get(f"/api/videos/{a}/memory-documents",params={"kind":"summary"}).json()["total"] > 0
    assert client.get(f"/api/videos/{b}/memory-documents",params={"kind":"summary"}).json()["total"] == 0
