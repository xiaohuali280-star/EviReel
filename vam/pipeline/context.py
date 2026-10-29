"""Everything a stage needs inside a worker process: settings, PostgreSQL ledger, object store,
the SQLite registry (read model + published per-video indexes on the shared DATA_ROOT volume),
model adapters, scratch space and immutable artifact helpers."""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import socket
import uuid
from pathlib import Path

from vam.memory.transcript import TranscriptStore
from vam.pipeline.repository import JobRepository
from vam.platform.object_store import ObjectStoreError
from vam.video_registry import DomainError, VideoRegistry

logger = logging.getLogger(__name__)


class StageError(Exception):
    def __init__(self, code, message, *, transient):
        super().__init__(message)
        self.code, self.message, self.transient = code, message, transient


def fault(point: str):
    """Fault injection for the E2 failure tests: VAM_PIPELINE_FAULT=<point>[,<point>...] makes this
    process die at that point exactly like `kill -9` (no cleanup, no ACK, no DB commit)."""
    points = {p.strip() for p in os.environ.get("VAM_PIPELINE_FAULT", "").split(",") if p.strip()}
    if point in points:
        # One-shot when a marker directory is given: a replacement process (e.g. the pool child
        # Celery forks after the crash) with the same environment must not crash again.
        marker_dir = os.environ.get("VAM_PIPELINE_FAULT_MARKER")
        if marker_dir:
            marker = Path(marker_dir) / point.replace(":", "_")
            try:
                with marker.open("x", encoding="utf-8") as handle:
                    handle.write(str(os.getpid()))
            except FileExistsError:
                return
        logger.warning("fault_injected point=%s pid=%s", point, os.getpid())
        os._exit(137)


def fault_once(point: str) -> bool:
    """Non-fatal test hook (e.g. "stall:vision"): True the first time `point` is reached when listed
    in VAM_PIPELINE_FAULT (one-shot via VAM_PIPELINE_FAULT_MARKER when set)."""
    points = {p.strip() for p in os.environ.get("VAM_PIPELINE_FAULT", "").split(",") if p.strip()}
    if point not in points:
        return False
    marker_dir = os.environ.get("VAM_PIPELINE_FAULT_MARKER")
    if marker_dir:
        try:
            with (Path(marker_dir) / point.replace(":", "_")).open("x", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
        except FileExistsError:
            return False
    logger.warning("fault_injected point=%s pid=%s", point, os.getpid())
    return True


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class WorkerContext:
    def __init__(self, settings, *, store=None, backend=None, transcriber=None):
        from vam.platform import build_platform
        self.settings = settings
        platform = build_platform(settings, store=store)
        self.db, self.store = platform.db, platform.store
        self.repo = JobRepository(self.db, settings)
        # No OwnerLock here: the API owns DATA_ROOT's lifecycle; workers only write attempt-keyed
        # paths and publish through conditional updates (see E2_DELIVERY "data boundaries").
        self.registry = VideoRegistry(settings.data_root)
        self.scratch = Path(settings.pipeline_scratch or (Path(settings.data_root) / ".pipeline")).resolve()
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.prefix = settings.s3_key_prefix.strip("/") + "/" if settings.s3_key_prefix.strip("/") else ""
        if settings.pipeline_factory:  # explicit test injection (never set in production config)
            module, name = settings.pipeline_factory.split(":")
            backend, transcriber = getattr(importlib.import_module(module), name)(settings)
        if backend is None:
            from vam.vision.backends import get_backend
            backend = get_backend()
        if transcriber is None:
            from vam.asr.transcriber import FasterWhisperTranscriber
            transcriber = FasterWhisperTranscriber(settings)
        self.backend, self.transcriber = backend, transcriber

    # ------------------------------------------------------------------ scratch & artifacts
    def workdir(self, token) -> Path:
        path = self.scratch / "attempts" / token
        path.mkdir(parents=True, exist_ok=True)
        return path

    def drop_workdir(self, token):
        shutil.rmtree(self.scratch / "attempts" / token, ignore_errors=True)

    def artifact_key(self, job, stage, token, name) -> str:
        # Immutable: one key per attempt, never overwritten; publication is a DB pointer update.
        return f"{self.prefix}pipeline/{job['video_id']}/{job['job_id']}/{stage}/{token}/{name}"

    def put_artifact(self, key, path, content_type="application/octet-stream") -> dict:
        sha = sha256_file(path)
        info = self.store.put_file(key, path, content_type)
        if info.size != Path(path).stat().st_size:
            raise StageError("artifact_incomplete", "Stored artifact size differs from the local file", transient=True)
        return {"key": key, "sha256": sha, "size": info.size}

    def put_json(self, key, value, path) -> dict:
        Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return self.put_artifact(key, path, "application/json")

    def fetch(self, ref: dict, path) -> Path:
        """Download an artifact and verify it is exactly the product that was recorded."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        try:
            with path.open("wb") as handle:
                for chunk in self.store.iter_object(ref["key"]):
                    digest.update(chunk)
                    handle.write(chunk)
        except ObjectStoreError as exc:
            raise StageError("artifact_unavailable", f"Cannot read artifact {ref['key']}", transient=True) from exc
        if digest.hexdigest() != ref["sha256"]:
            raise StageError("artifact_corrupt", f"Artifact {ref['key']} does not match its recorded sha256", transient=False)
        return path

    def fetch_json(self, ref, path):
        return json.loads(self.fetch(ref, path).read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ inputs
    def source(self, video_id, token) -> Path:
        """The verified original by object reference when it has one (uploads), else the local file (legacy)."""
        with self.db.connect() as conn:
            owned = conn.execute("SELECT object_key, sha256 FROM video_ownership WHERE video_id=%s", (video_id,)).fetchone()
        row = self.registry.get(video_id)
        if owned and owned["object_key"] and owned["sha256"]:
            cached = self.scratch / "sources" / f"{owned['sha256']}{Path(row['source_path']).suffix}"
            if cached.is_file() and sha256_file(cached) == owned["sha256"]:
                return cached
            tmp = self.workdir(token) / ("source" + Path(row["source_path"]).suffix)
            self.fetch({"key": owned["object_key"], "sha256": owned["sha256"]}, tmp)
            cached.parent.mkdir(parents=True, exist_ok=True)
            tmp.replace(cached)
            return cached
        path = self.registry.asset(video_id, row["source_path"])
        if not path.is_file():
            raise StageError("source_missing", "Video source file is missing", transient=False)
        return path

    # ------------------------------------------------------------------ registry read model (single writer: workers)
    def project(self, job, **fields):
        """Mirror job progress into SQLite index_jobs, which the existing UI/API read. Never a source of truth."""
        self.registry.project_job(job["job_id"], job["video_id"], job["kind"])
        if fields:
            self.registry.update_job(job["job_id"], **fields)

    def transcript_store(self, video_id, candidate):
        row = self.registry.get(video_id)
        generation = row["transcript_generation"] or candidate
        if not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise DomainError(409, "invalid_index_version", "Invalid transcript generation")
        store = TranscriptStore(self.registry.asset(video_id, f"memory/{generation}.sqlite3"), video_id)
        if not row["transcript_generation"]:
            self.registry.update_video(video_id, transcript_generation=generation)
        return store

    def drop_generation(self, video_id, generation):
        """Delete a local index generation that is not published (attempt-keyed, safe to remove)."""
        row = self.registry.get(video_id)
        if generation in {row["active_generation"], row["transcript_generation"]}:
            return
        path = self.registry.asset(video_id, f"memory/{generation}.sqlite3")
        for suffix in ("", "-wal", "-shm", ".lock"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        assets = self.registry.asset(video_id, f"cache/{generation}")
        if assets.exists():
            shutil.rmtree(assets, ignore_errors=True)
