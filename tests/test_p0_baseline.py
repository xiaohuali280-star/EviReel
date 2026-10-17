"""P0 cloud characterization tests. No paid API and no production changes."""
import asyncio
import base64
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
from PIL import Image
from fastapi.testclient import TestClient

from vam.retrieval.frame_store import FrameStore
from vam.retrieval.agent_search import hybrid_event_frame_search, search_generator
from vam.vision.backends import OpenRouterGeminiBackend
from vam.video import extract_data_uris_ffmpeg


def image(color):
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


class MockEmbedding:
    name = "p0-deterministic-3d"

    def embed_text(self, text, *, instruction):
        return [1.0, 0.0, 0.0], 3

    def embed_texts_batch(self, texts, *, instruction, batch_size):
        return [[1.0, 0.0, 0.0] for _ in texts], 3

    def embed_image_base64(self, value, *, instruction):
        raw = base64.b64decode(value.split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as img:
            pixel = np.asarray(img.convert("RGB"))[0, 0]
        idx = int(np.argmax(pixel))
        return [float(i == idx) for i in range(3)], 3

    def embed_images_base64_batch(self, images, *, instruction, batch_size):
        return [self.embed_image_base64(x, instruction=instruction)[0] for x in images], 3


class StoreBaseline(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = MockEmbedding()
        self.store = FrameStore(backend=self.backend, persist_path=str(Path(self.tmp.name) / "a.sqlite3"))
        self.store._vlm_client.chat = AsyncMock(return_value=("Attention formula on the slide.", {}))

    async def seed(self, times=(0.0, 10.0, 20.0), store=None):
        target = store or self.store
        return await target.add_frames(
            ts=list(times), images_base64=[image(c) for c in ("red", "lime", "blue")],
            frame_ids=["f0", "f1", "f2"], batch_size=3,
        )

    async def test_separate_databases_survive_reload_without_cross_contamination(self):
        await self.store.add_frames(ts=[10.0], images_base64=[image("red")], frame_ids=["video-a"])
        other = FrameStore(backend=self.backend, persist_path=str(Path(self.tmp.name) / "b.sqlite3"))
        await other.add_frames(ts=[20.0], images_base64=[image("blue")], frame_ids=["video-b"])
        reloaded = FrameStore(backend=self.backend, persist_path=self.store._persist_path)
        self.assertEqual([f.frame_id for f in await reloaded.list_frames()], ["video-a"])
        self.assertEqual([f.frame_id for f in await other.list_frames()], ["video-b"])
        self.assertEqual(reloaded._dim, 3)

    async def test_batch_rejects_incompatible_embedding_dimensions(self):
        await self.seed()
        with patch.object(self.backend, "embed_images_base64_batch", return_value=([[1.0, 0.0]], 2)):
            with self.assertRaisesRegex(RuntimeError, "embedding dim mismatch"):
                await self.store.add_frames(ts=[30.0], images_base64=[image("red")], frame_ids=["bad"])
        self.assertEqual(len(await self.store.list_frames()), 3)

    async def test_one_hour_is_recent_on_video_timeline(self):
        await self.seed((0.0, 1800.0, 3600.0))
        result = await self.store.apply_retention_policy()
        self.assertEqual(result["time_axis"], "relative")
        self.assertEqual({f.memory_tier for f in await self.store.list_frames()}, {"recent"})

    async def test_long_timeline_applies_distinct_tiers(self):
        await self.seed((0.0, 4000.0, 90000.0))
        await self.store.apply_retention_policy()
        self.assertEqual([f.memory_tier for f in await self.store.list_frames()], ["long", "mid", "recent"])

    async def test_event_has_timestamp_links_and_does_not_generate_summary(self):
        frames = await self.seed()
        event = await self.store.finalize_event_memory(frames=frames)
        self.assertEqual((event.layer, event.kind), ("long", "event"))
        self.assertEqual((event.start_t, event.end_t), (0.0, 20.0))
        self.assertEqual(event.frame_ids, ["f0", "f1", "f2"])
        self.assertEqual(event.representative_frame_id, "f2")
        self.assertFalse(any(d.kind == "summary" for d in await self.store.list_memory_documents()))

    async def test_explicit_summary_upserts_same_window(self):
        await self.seed()
        first = await self.store.summarize_time_range(min_time=0, max_time=30, time_mode="relative", granularity_s=30)
        second = await self.store.summarize_time_range(min_time=0, max_time=30, time_mode="relative", granularity_s=30)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].doc_id, second[0].doc_id)
        self.assertEqual(first[0].layer, "summary")

    async def test_hybrid_sources_and_time_range_are_enforced(self):
        frames = await self.seed()
        event = await self.store.finalize_event_memory(frames=frames)
        hits = await hybrid_event_frame_search("attention", store_instance=self.store, sources=["event"], min_time=0, max_time=25, time_mode="relative")
        self.assertEqual([h["doc_id"] for h in hits], [event.doc_id])
        self.assertEqual(hits[0]["time_range"]["end_t"], 20.0)
        self.assertEqual(await hybrid_event_frame_search("attention", store_instance=self.store, sources=["frame"], min_time=100, max_time=110, time_mode="relative"), [])

    async def test_mock_planner_search_inspect_answer_preserves_history_and_evidence(self):
        await self.seed()
        plans = [
            {"action": "search", "queries": [{"q": "attention", "top_k": 1, "inspect_k": 0}], "sources": ["frame"]},
            {"action": "inspect", "prompt": "Read the formula", "ref": {"turn_idx": 1, "result_idx": 0}},
            {"action": "answer", "response": "The attention formula is at 0 seconds.", "best_ref": {"turn_idx": 1, "result_idx": 0}},
        ]
        mock_chat = AsyncMock(side_effect=[(json.dumps(p), {}) for p in plans])
        history = [{"role": "user", "content": "Where is the attention formula?"}]
        with patch("vam.retrieval.agent_search.OpenRouterClient") as client:
            client.return_value.chat = mock_chat
            events = [e async for e in search_generator("Explain this formula", store_instance=self.store, chat_history=history)]
        final = next(e["result"] for e in events if e["type"] == "final")
        self.assertTrue(final["found"])
        self.assertEqual(final["best"]["t"], 0.0)
        self.assertTrue(any(e["type"] == "tool_result" and e["tool"] == "inspect" for e in events))
        self.assertIn("Where is the attention formula?", str(mock_chat.call_args_list[0]))
        self.assertEqual(mock_chat.await_count, 3)

    async def test_retention_tier_changes_survive_reload_after_event_finalization(self):
        """Desired contract: this can reveal an upstream tier-only persistence defect."""
        frames = await self.seed()
        # Isolate the separate upstream missing-re import; production remains untouched.
        with patch("vam.retrieval.frame_store.re", re, create=True):
            await self.store.finalize_event_memory(frames=frames)
        await self.store.apply_retention_policy()
        reloaded = FrameStore(backend=self.backend, persist_path=self.store._persist_path)
        self.assertEqual([f.memory_tier for f in await reloaded.list_frames()], ["recent"] * 3)

    async def test_real_backend_adapter_single_image_returns_one_dimensional_vector(self):
        """Real adapter, mocked HTTP response; no real model validation."""
        adapter = OpenRouterGeminiBackend()
        target = FrameStore(backend=adapter, persist_path=str(Path(self.tmp.name) / "adapter.sqlite3"))
        with patch("vam.llm.openrouter.OpenRouterEmbeddingsClient._post", return_value={"data": [{"embedding": [1.0, 0.0, 0.0]}]}):
            frame = await target.add_frame(t=0.0, image_base64=image("red"), frame_id="adapter")
        self.assertEqual(frame.img_emb.ndim, 1)
        self.assertEqual(target._dim, 3)


class TransportAndVideoBaseline(unittest.TestCase):
    def test_websocket_keeps_connection_history_but_reconnect_starts_empty(self):
        module = importlib.import_module("vam.server.ws.agent_chat")
        from vam.server.app import create_app
        lengths = []

        async def fake_response(*, transcript, history):
            lengths.append(len(history))
            history.extend([{"role": "user", "content": transcript}, {"role": "assistant", "content": "mock"}])
            yield {"type": "final", "text": "mock", "result": {"answer": "mock"}}

        with patch.object(module, "stream_agent_response", fake_response), TestClient(create_app()) as client:
            with client.websocket_connect("/ws/agent") as ws:
                self.assertEqual(ws.receive_json()["type"], "info")
                for text in ("question", "follow-up"):
                    ws.send_json({"type": "text", "text": text})
                    self.assertEqual(ws.receive_json()["type"], "final")
            with client.websocket_connect("/ws/agent") as ws:
                ws.receive_json()
                ws.send_json({"type": "text", "text": "reconnect"})
                self.assertEqual(ws.receive_json()["type"], "final")
        self.assertEqual(lengths, [0, 2, 0])

    def test_ffmpeg_extracts_timestamped_frames_from_synthetic_video(self):
        self.assertIsNotNone(shutil.which("ffmpeg"))
        self.assertIsNotNone(shutil.which("ffprobe"))
        with tempfile.TemporaryDirectory() as tmp:
            video = str(Path(tmp) / "synthetic.mp4")
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=4", "-t", "2", "-c:v", "mpeg4", "-y", video], check=True)
            frames, source_fps, truncated, report = extract_data_uris_ffmpeg(video, 1.0, None, 0.0, 0.0, 1.1, 0.0)
        self.assertGreaterEqual(len(frames), 1)
        self.assertEqual(source_fps, 4.0)
        self.assertEqual(frames[0][0], 0.0)
        self.assertTrue(all(0 <= t < 2.0 and uri.startswith("data:image/") for t, uri in frames))
        self.assertFalse(truncated)


if __name__ == "__main__":
    unittest.main()
