from __future__ import annotations

import asyncio
import errno
import logging
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from vam.runtime import run_sync

from vam.indexing.media import ALLOWED_EXTENSIONS, inspect_upload, prepare_playback
from vam.retrieval.frame_store import FrameStore
from vam.storage.database import OwnerLock
from vam.video import index_video_async
from vam.video_registry import DomainError, VideoRegistry
from vam.vision.backends import get_backend

logger = logging.getLogger(__name__)

class PersistentProgress:
    """Reuse the existing indexing pipeline without publishing a staging index early."""
    def __init__(self, registry):
        self.registry = registry

    def log(self, job_id, msg):
        job = self.registry.job(job_id)
        self.registry.update_job(job_id, logs=(job["logs"] + [str(msg)[:600]])[-100:])

    def progress(self, job_id, *, phase=None, current=None, total=None):
        fields = {"status": "running"}
        fields.update({k: v for k, v in {"phase": phase, "current": current, "total": total}.items() if v is not None})
        self.registry.update_job(job_id, **fields)

    def done(self, job_id, *, result):
        self.registry.update_job(job_id, phase="publishing", result=result)

    def fail(self, job_id, *, error):
        self.registry.update_job(job_id, status="failed", phase="failed", error_code="index_failed", error=str(error)[:800])

class IndexingService:
    def __init__(self, settings, *, backend=None, indexer=None):
        self.settings = settings
        self.registry = VideoRegistry(settings.data_root)
        self.backend = backend
        self.indexer = indexer or index_video_async
        self.progress = PersistentProgress(self.registry)
        self.owner = OwnerLock(self.registry.root)
        self.semaphore = asyncio.Semaphore(settings.index_concurrency)
        self.tasks = {}
        self.leases = {}
        self.memory_locks = {}
        self.closed = False

    async def start(self):
        self.owner.acquire()
        try:
            self.registry.recover()
            for path in (self.registry.root / ".uploads").glob("*.part"):
                path.unlink(missing_ok=True)
            # Resume only filesystem deletion, never automatically restart model work.
            with self.registry.db.connect() as conn:
                deleting = [r[0] for r in conn.execute("SELECT video_id FROM videos WHERE status='deleting'")]
            for video_id in deleting:
                self.delete(video_id)
        except BaseException:
            self.owner.release()
            raise

    async def close(self):
        self.closed = True
        for task in list(self.tasks.values()):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        self.owner.release()

    def _usable_video(self, video_id):
        row = self.registry.get(video_id)
        if row["status"] in {"deleting", "delete_failed"}:
            raise DomainError(409, "video_deleting", "Video is being deleted")
        return row

    def _retain(self, video_id):
        self.leases[video_id] = self.leases.get(video_id, 0) + 1
        released = False
        def release():
            nonlocal released
            if not released:
                self.leases[video_id] -= 1
                if not self.leases[video_id]:
                    self.leases.pop(video_id, None)
                released = True
        return release

    async def ingest(self, upload):
        name = str(upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            await upload.close()
            raise DomainError(415, "unsupported_media", "Supported video extensions: mp4, m4v, mov, mkv, webm, avi")
        if upload.content_type and not (upload.content_type.startswith("video/") or upload.content_type == "application/octet-stream"):
            await upload.close()
            raise DomainError(415, "unsupported_media", "Expected a video upload")
        video_id = "v_" + uuid.uuid4().hex
        temporary = self.registry.root / ".uploads" / (uuid.uuid4().hex + ".part")
        directory = self.registry.directory(video_id)
        registered = False
        size = 0
        try:
            with temporary.open("xb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.settings.max_upload_bytes:
                        raise DomainError(413, "upload_limit", "Upload exceeds configured size limit")
                    handle.write(chunk)
            if not size:
                raise DomainError(422, "empty_upload", "Video upload is empty")
            meta = await run_sync(inspect_upload, temporary, self.settings)
            directory.mkdir()
            temporary.replace(directory / ("source" + suffix))
            row = self.registry.register(video_id, name[:255], "source" + suffix, size, meta["duration"])
            registered = True
            try:
                job_id = self.schedule(video_id)
                return {"video_id": video_id, "job_id": job_id, "video": self.registry.public(self.registry.get(video_id))}
            except DomainError as exc:
                # Upload is a durable success even when the bounded queue is full.
                return {"video_id": video_id, "job_id": None, "video": self.registry.public(row),
                        "index_error": {"code": exc.code, "message": exc.message}}
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise DomainError(507, "storage_full", "Insufficient video storage") from exc
            raise DomainError(500, "storage_error", "Video storage operation failed") from exc
        finally:
            await upload.close()
            temporary.unlink(missing_ok=True)
            if not registered and directory.exists():
                shutil.rmtree(self.registry.directory(video_id))

    def schedule(self, video_id):
        if self.closed:
            raise DomainError(503, "server_stopping", "Server is stopping")
        self._usable_video(video_id)
        if self.leases.get(video_id):
            raise DomainError(409, "video_busy", "Video is in use; retry indexing later")
        job_id = self.registry.create_job(video_id, self.settings.max_queued_jobs)
        task = asyncio.create_task(self._run(video_id, job_id))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))
        return job_id

    def _store(self, video_id, generation):
        if not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise DomainError(409, "invalid_index_version", "Invalid stored index version")
        path = self.registry.asset(video_id, f"memory/{generation}.sqlite3")
        try:
            store = FrameStore(backend=self.backend or get_backend(), persist_path=str(path))
        except RuntimeError as exc:
            raise DomainError(409, "incompatible_index", "Index model/schema is incompatible; back up and rebuild this video index") from exc
        assets = self.registry.asset(video_id, f"cache/{generation}")
        assets.mkdir(parents=True, exist_ok=True)
        store.asset_root = str(assets)
        return store

    async def _run(self, video_id, job_id):
        logger.info("index_queued video_id=%s job_id=%s", video_id, job_id)
        try:
            async with self.semaphore:
                row = self.registry.get(video_id)
                self.registry.update_video(video_id, status="running", last_error=None)
                self.registry.update_job(job_id, status="running", phase="playback")
                source = self.registry.asset(video_id, row["source_path"])
                playback = await run_sync(prepare_playback, source, self.registry.directory(video_id), self.settings)
                self.registry.update_video(video_id, playback_path=playback.name)
                store = self._store(video_id, job_id)
                cfg = self.settings
                await self.indexer(
                    tmp_path=str(source), cleanup_temp=False, fps=cfg.video_fps, max_frames=cfg.video_max_frames,
                    laplacian_min=cfg.video_laplacian_min, diff_threshold=cfg.video_diff_threshold,
                    ssim_threshold=cfg.video_ssim_threshold, hist_threshold=cfg.video_hist_threshold,
                    similarity_threshold=cfg.video_similarity_threshold, frame_id_prefix="v",
                    video_absolute_start_time=None, debug=False, store_instance=store, manager_instance=self.progress,
                    existing_job_id=job_id, cache_root=store.asset_root, run_inline=True,
                )
                job = self.registry.job(job_id)
                if job["status"] == "failed":
                    raise RuntimeError(job["error"] or "index failed")
                result = job["result"] or {}
                if result.get("count", 0) <= 0 or not await store.list_frames():
                    raise RuntimeError("index contains no usable frames")
                partial = bool(result.get("truncated") or result.get("filter_report", {}).get("embed_failures", {}).get("count"))
                result["video_id"] = video_id
                result["capabilities"] = {"playback": True, "vision": True, "transcript": False}
                result["model"] = {"fingerprint": store.model_fingerprint(), "dimension": store._dim}
                self.registry.publish(video_id, job_id, store.model_fingerprint(), result, partial)
                logger.info("index_complete video_id=%s job_id=%s partial=%s", video_id, job_id, partial)
        except asyncio.CancelledError:
            self.registry.update_job(job_id, status="interrupted", phase="interrupted",
                                     error_code="server_shutdown", error="Server stopped before indexing finished")
            self.registry.update_video(video_id, status="interrupted", last_error="Index interrupted; retry is available")
            raise
        except Exception as exc:
            logger.warning("index_failed video_id=%s job_id=%s error_type=%s", video_id, job_id, type(exc).__name__)
            self.registry.update_job(job_id, status="failed", phase="failed", error_code="index_failed", error=str(exc)[:800])
            self.registry.update_video(video_id, status="failed", last_error="Indexing failed; see job details and retry")
        finally:
            job = self.registry.job(job_id)
            if job["status"] in {"failed", "interrupted"}:
                # No caller can reference an unpublished generation.
                path = self.registry.asset(video_id, f"memory/{job_id}.sqlite3")
                for suffix in ("", "-wal", "-shm", ".lock"):
                    Path(str(path) + suffix).unlink(missing_ok=True)
                assets = self.registry.asset(video_id, f"cache/{job_id}")
                if assets.exists():
                    shutil.rmtree(assets)

    @asynccontextmanager
    async def memory_context(self, video_id):
        row = self._usable_video(video_id)
        generation = row["active_generation"]
        if not generation:
            raise DomainError(409, "index_not_ready", "Video has no usable visual index")
        release = self._retain(video_id)
        lock = self.memory_locks.setdefault(video_id, asyncio.Lock())
        try:
            async with lock:
                yield row, self._store(video_id, generation)
        finally:
            release()

    def playback_asset(self, video_id):
        row = self._usable_video(video_id)
        if not row["playback_path"]:
            raise DomainError(409, "playback_not_ready", "Video playback is not ready")
        path = self.registry.asset(video_id, row["playback_path"])
        if not path.is_file():
            raise DomainError(404, "asset_missing", "Video asset is missing")
        return path, self._retain(video_id)

    def delete(self, video_id):
        self.registry.get(video_id)
        if self.leases.get(video_id):
            raise DomainError(409, "video_busy", "Video is in use")
        with self.registry.db.connect() as conn:
            if conn.execute("SELECT 1 FROM index_jobs WHERE video_id=? AND status IN ('queued','running')", (video_id,)).fetchone():
                raise DomainError(409, "index_running", "Wait for indexing to finish before deleting")
        directory = self.registry.directory(video_id)
        self.registry.update_video(video_id, status="deleting")
        try:
            if directory.exists():
                shutil.rmtree(directory)
            with self.registry.db.connect(write=True) as conn:
                conn.execute("DELETE FROM videos WHERE video_id=?", (video_id,))
            self.memory_locks.pop(video_id, None)
        except OSError as exc:
            self.registry.update_video(video_id, status="delete_failed", last_error="Deletion incomplete; retry deletion")
            raise DomainError(500, "delete_failed", "Video deletion incomplete; retry deletion") from exc
