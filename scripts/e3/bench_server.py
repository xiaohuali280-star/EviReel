"""E3 benchmark server: real API + real SQLite indexes, synthetic data, MODEL SIMULATOR (no provider calls).

Never imported by the production app. It seeds N videos with a synthetic but realistically sized index
(frames with JPEG payloads + embeddings, event documents, transcript chunks) and serves the normal API.
Every model call is replaced by a simulator with a fixed latency, so measured times separate
lock waits, index loading, SQLite scans, simulated model time and event-loop lag.

It instruments only through public seams that exist before and after E3, so the same script measures the
baseline commit and the E3 code:
  * asyncio locks handed out by ``IndexingService.memory_locks`` (lock wait),
  * ``FrameStore.__init__`` (index load), ``FrameStore._fetch_*_sync`` and ``TranscriptStore.search`` (scan),
  * the simulated embedding/VLM latency (model time), a 50 ms ticker (event-loop lag).

  python scripts/e3/bench_server.py --port 8890 --videos 4 --frames 300 --dim 1024 --model-ms 40
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).parents[2]))
for key in ('SILICONFLOW_API_KEY', 'OPENROUTER_API_KEY', 'DASHSCOPE_API_KEY'):
    os.environ[key] = ''

import numpy as np
from PIL import Image
import uvicorn

import vam.config as config
from vam.config import Settings

STATS_LOCK = threading.Lock()
STATS: dict[str, list[float]] = {}


def record(name, seconds):
    with STATS_LOCK:
        STATS.setdefault(name, []).append(seconds)


def summary():
    out = {}
    with STATS_LOCK:
        for name, values in STATS.items():
            ordered = sorted(values)
            pick = lambda q: ordered[min(len(ordered) - 1, int(math.ceil(q * len(ordered))) - 1)] if ordered else 0
            out[name] = {'count': len(values), 'total_s': round(sum(values), 4), 'mean_ms': round(1000 * sum(values) / len(values), 3),
                         'p50_ms': round(1000 * pick(.5), 3), 'p95_ms': round(1000 * pick(.95), 3),
                         'p99_ms': round(1000 * pick(.99), 3), 'max_ms': round(1000 * ordered[-1], 3)}
    return out


class SimBackend:
    """MODEL SIMULATOR: deterministic unit vectors, fixed latency per call (time.sleep in the worker thread,
    like the real synchronous HTTP embedding client)."""
    name = 'e3-model-simulator'

    def __init__(self, dim, latency_s):
        self.dim, self.latency_s = dim, latency_s

    def fingerprint(self):
        return {'backend': self.name, 'dim': self.dim, 'encoding_version': 1}

    def _vector(self, text):
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], 'little')
        vec = np.random.default_rng(seed).standard_normal(self.dim).astype(np.float32)
        return (vec / np.linalg.norm(vec)).tolist()

    def _call(self, n):
        started = time.perf_counter()
        time.sleep(self.latency_s)
        record('model_embed', time.perf_counter() - started)

    def embed_text(self, text, *, instruction):
        self._call(1)
        return self._vector(text), self.dim

    def embed_texts_batch(self, texts, *, instruction, batch_size):
        self._call(len(texts))
        return [self._vector(t) for t in texts], self.dim

    def embed_image_base64(self, image, *, instruction):
        self._call(1)
        return self._vector(image[:64]), self.dim

    def embed_images_base64_batch(self, images, *, instruction, batch_size):
        self._call(len(images))
        return [self._vector(i[:64]) for i in images], self.dim


def jpeg_payload(rng, width=640, height=360):
    base = np.linspace(0, 255, width * height * 3, dtype=np.float32).reshape(height, width, 3)
    noise = rng.integers(0, 60, size=(height, width, 3))
    image = Image.fromarray(np.clip(base + noise, 0, 255).astype(np.uint8))
    buffer = io.BytesIO()
    image.save(buffer, 'JPEG', quality=70)
    import base64
    return 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode()


def seed(service, backend, *, videos, frames, events, chunks, rng):
    """Write a published generation per video exactly as the indexers leave it (SQLite schema of FrameStore +
    TranscriptStore), then point the registry at it."""
    from vam.memory.transcript import TranscriptStore, embedding_fingerprint
    from vam.asr.chunker import Segment, Chunk
    from vam.retrieval.frame_store import FrameStore, _tensor_to_blob
    import torch
    registry = service.registry
    payloads = [jpeg_payload(rng) for _ in range(12)]
    created = []
    for v in range(videos):
        video_id = 'v_' + uuid.uuid4().hex
        generation = uuid.uuid4().hex
        directory = registry.directory(video_id)
        directory.mkdir(parents=True)
        (directory / 'source.mp4').write_bytes(b'0' * 1024)
        duration = frames * 2.0
        registry.register(video_id, f'bench-{v}.mp4', 'source.mp4', 1024, duration)
        path = registry.asset(video_id, f'memory/{generation}.sqlite3')
        path.parent.mkdir(parents=True, exist_ok=True)
        store = FrameStore(backend=backend, persist_path=str(path))   # creates the schema
        vec = lambda text: torch.tensor(backend._vector(text), dtype=torch.float16)
        with sqlite3.connect(path) as conn:
            conn.executemany('INSERT INTO frames VALUES (?,?,?,?,?,?,?)',
                             [(f'v{i:05d}', i * 2.0, None, payloads[i % len(payloads)], _tensor_to_blob(vec(f'frame {v} {i}')),
                               'long', f'event-{i // max(1, frames // events)}') for i in range(frames)])
            span = duration / events
            conn.executemany('INSERT INTO memory_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                             [(uuid.uuid4().hex, 'long', 'event', e * span, (e + 1) * span, None, None,
                               f'Lecture event {e}: spanning tree, graph theory topic {e % 7}', f'v{int(e * span / 2):05d}',
                               json.dumps([f'v{int(e * span / 2):05d}']), _tensor_to_blob(vec(f'event {v} {e}')), None)
                              for e in range(events)])
            conn.execute('UPDATE store_meta SET backend=?, dim=?, revision=1, model_fingerprint=? WHERE store_key=1',
                         (backend.name, backend.dim, store.model_fingerprint()))
        transcript = TranscriptStore(path, video_id)
        version = uuid.uuid4().hex
        seg_span = duration / chunks
        segments = [Segment(f's{i}', i * seg_span, (i + 1) * seg_span, f'teacher says topic {i % 11} about trees', 'en',
                            {}, f'teacher says topic {i % 11}', i * seg_span, (i + 1) * seg_span, False) for i in range(chunks)]
        chunk_rows = [Chunk(f'c{i:05d}', s.start_t, s.end_t, s.text, [s.segment_id]) for i, s in enumerate(segments)]
        transcript.stage(version=version, transcription_key='bench', source_sha256='0' * 64, asr_fingerprint='bench',
                         chunk_config='bench', embedding_identity=embedding_fingerprint(backend), segments=segments,
                         chunks=chunk_rows, language='en', metadata={})
        transcript.publish(version, vectors=[backend._vector(c.text) for c in chunk_rows], dim=backend.dim)
        registry.update_video(video_id, status='ready', active_generation=generation, transcript_generation=generation,
                              transcript_status='ready', playback_path='source.mp4')
        created.append({'video_id': video_id, 'generation': generation})
    return created


def instrument():
    """Wrap timing around seams that exist in the baseline and in E3 (missing seams are skipped)."""
    from vam.retrieval import frame_store as fs
    from vam.memory import transcript as tr

    def timed(owner, name, label):
        original = getattr(owner, name, None)
        if original is None:
            return
        def wrapper(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                record(label, time.perf_counter() - started)
        setattr(owner, name, wrapper)

    timed(fs.FrameStore, '__init__', 'index_open')
    timed(fs.FrameStore, '_fetch_frames_sync', 'sqlite_frames')
    timed(fs.FrameStore, '_fetch_documents_sync', 'sqlite_documents')
    timed(tr.TranscriptStore, 'search', 'sqlite_transcript_search')


class TimedLock(asyncio.Lock):
    async def acquire(self):
        started = time.perf_counter()
        try:
            return await super().acquire()
        finally:
            record('lock_wait', time.perf_counter() - started)


class TimedLocks(dict):
    """Drop-in for IndexingService.memory_locks: every handed-out lock records its wait time."""
    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = TimedLock()
        return self[key]


async def loop_lag_monitor(interval=.05):
    while True:
        started = time.perf_counter()
        await asyncio.sleep(interval)
        record('event_loop_lag', max(0.0, time.perf_counter() - started - interval))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8890)
    parser.add_argument('--videos', type=int, default=4)
    parser.add_argument('--frames', type=int, default=300)
    parser.add_argument('--events', type=int, default=60)
    parser.add_argument('--chunks', type=int, default=200)
    parser.add_argument('--dim', type=int, default=1024)
    parser.add_argument('--model-ms', type=float, default=40.0)
    parser.add_argument('--vlm-ms', type=float, default=400.0)
    parser.add_argument('--data-root', default='')
    parser.add_argument('--out', default='', help='write the seeded video ids as JSON here')
    args = parser.parse_args()
    temporary = tempfile.mkdtemp(prefix='evireel-e3-bench-')
    root = Path(args.data_root or temporary)
    settings = Settings(_env_file=None, data_root=str(root / 'assets'), enable_retrieval=True, rerank_enabled=False,
                        query_include_latest_segment_video=False, frame_store_path=str(root / 'default.sqlite3'),
                        study_auto_generate=False)
    config._settings = settings
    backend = SimBackend(args.dim, args.model_ms / 1000)

    from vam.llm.openrouter import OpenRouterClient

    async def simulated_vlm(self, messages, **kwargs):
        started = time.perf_counter()
        await asyncio.sleep(args.vlm_ms / 1000)
        record('model_vlm', time.perf_counter() - started)
        return 'Simulated summary of the window: spanning trees and graph search.', {}

    OpenRouterClient.chat = simulated_vlm
    instrument()
    from vam.server.app import create_app
    app = create_app(settings=settings, backend=backend)
    rng = np.random.default_rng(7)
    state, ready = {}, asyncio.Lock()

    async def setup():
        # The app uses a lifespan handler (on_event hooks would not run): seed once, on first use.
        async with ready:
            if 'videos' in state:
                return
            service = app.state.indexing
            if hasattr(service, 'memory_locks'):
                service.memory_locks = TimedLocks()
            state['videos'] = await asyncio.to_thread(seed, service, backend, videos=args.videos, frames=args.frames,
                                                      events=args.events, chunks=args.chunks, rng=rng)
            state['monitor'] = asyncio.create_task(loop_lag_monitor())
            if args.out:
                Path(args.out).write_text(json.dumps(state['videos']), encoding='utf-8')

    @app.get('/__bench__/videos')
    async def videos():
        await setup()
        return {'videos': state.get('videos', []), 'config': vars(args), 'mode': 'explicit-model-simulator'}

    resources = {'cpu': time.process_time(), 'wall': time.perf_counter()}

    @app.get('/__bench__/metrics')
    async def metrics():
        out = summary()
        wall = time.perf_counter() - resources['wall']
        out['process'] = {'cpu_s': round(time.process_time() - resources['cpu'], 3), 'wall_s': round(wall, 3),
                          'cpu_utilization_cores': round((time.process_time() - resources['cpu']) / wall, 3) if wall else None}
        try:
            import psutil
            out['process']['rss_mb'] = round(psutil.Process().memory_info().rss / 2**20, 1)
        except ImportError:
            out['process']['rss_mb'] = None
        return out

    @app.post('/__bench__/reset')
    async def reset():
        with STATS_LOCK:
            STATS.clear()
        resources.update(cpu=time.process_time(), wall=time.perf_counter())
        return {'ok': True}

    @app.post('/__bench__/summarize/{video_id}')
    async def summarize(video_id: str, start: float = 0, end: float = 60, granularity: float = 30):
        """The agent's summarize tool path (simulated VLM): derived summary write while readers run."""
        service = app.state.indexing
        started = time.perf_counter()
        writer = getattr(service, 'summary_context', None)
        context = writer(video_id) if writer else service.retrieval_context(video_id)
        async with context as scope:
            store = scope.visual if hasattr(scope, 'visual') else scope
            created = await store.summarize_time_range(min_time=start, max_time=end, time_mode='relative',
                                                       granularity_s=granularity, prompt='bench')
        record('summarize_request', time.perf_counter() - started)
        return {'created': len(created)}

    print(f'E3 BENCH SERVER (model simulator) http://127.0.0.1:{args.port} data={root}', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
