from __future__ import annotations

import asyncio
import errno
import logging
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from vam.coordination import LocalCoordinator
from vam.memory.transcript import embedding_fingerprint
from vam.runtime import run_sync
from vam.asr.audio import ASRError
from vam.asr.indexer import checksum, index_transcript
from vam.asr.transcriber import FasterWhisperTranscriber
from vam.memory.transcript import TranscriptStore

from vam.indexing.media import ALLOWED_EXTENSIONS, inspect_upload, prepare_playback
from vam.retrieval.frame_store import FrameStore
from vam.storage.database import OwnerLock
from vam.storage.pg_registry import registry_database
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
        self.registry.update_job(job_id, status="running", phase="vision_failed", error_code="index_failed", error=str(error)[:800])

@dataclass(frozen=True)
class ReadSnapshot:
    """What one request reads, fixed at its start (E3): the published visual generation, the transcript file
    and its text/search versions, and the embedding identity. The request never switches to a newer
    publication midway; the read lease keeps the pinned generation on disk until it finishes."""
    video_id: str
    visual_generation: str | None
    transcript_generation: str | None
    transcript_text_version: str | None
    transcript_search_version: str | None
    embedding_identity: str

    def as_dict(self):
        return {'visual': self.visual_generation, 'transcript_file': self.transcript_generation,
                'transcript_text': self.transcript_text_version, 'transcript_search': self.transcript_search_version,
                'embedding_identity': self.embedding_identity}


class ReaderCache:
    """Read-only handles of published generations, shared by this process's concurrent requests.
    Keyed by (video, generation, embedding identity): a generation never changes after publication."""
    def __init__(self, entries):
        self.entries = entries
        self.items: OrderedDict = OrderedDict()
        self.lock = threading.Lock()
        self.opens = self.hits = 0

    def get(self, key, opener):
        with self.lock:
            store = self.items.get(key)
            if store is not None:
                self.items.move_to_end(key)
                self.hits += 1
                return store
        store = opener()   # outside the lock: opening reads the file
        with self.lock:
            self.opens += 1
            if self.entries <= 0:
                return store
            existing = self.items.setdefault(key, store)
            self.items.move_to_end(key)
            while len(self.items) > self.entries:
                self.items.popitem(last=False)
            return existing

    def evict(self, video_id, generation=None):
        with self.lock:
            for key in [k for k in self.items if k[0] == video_id and (generation is None or k[1] == generation)]:
                del self.items[key]


class IndexingService:
    def __init__(self, settings, *, backend=None, indexer=None, transcriber=None, reranker=None):
        self.settings = settings
        self.registry = VideoRegistry(settings.data_root, registry_database(settings, settings.data_root))
        self.backend = backend
        self.indexer = indexer or index_video_async
        self.transcriber = transcriber or FasterWhisperTranscriber(settings)
        self.reranker = reranker
        self.progress = PersistentProgress(self.registry)
        # Several API instances share DATA_ROOT only when coordination is external (E3); otherwise keep the
        # single-owner guard (in-process indexing recovers jobs at start and must not run twice).
        self.owner = None if settings.api_multi_instance else OwnerLock(self.registry.root)
        self.semaphore = asyncio.Semaphore(settings.index_concurrency)
        self.tasks = {}
        # Read leases / tombstones / retired generations. The app swaps in PgCoordinator in platform mode.
        self.coordination = LocalCoordinator(settings)
        self.readers = ReaderCache(settings.reader_cache_entries)
        self.on_reclaimed = None   # platform: release ownership + quota after a deletion is reclaimed
        # Platform (set by the app): published files are restored from object storage when DATA_ROOT lacks
        # them (vam.publication), and their object keys are queued for deletion with the video.
        self.publications = None
        self.queue_objects = None
        # Versioned query-vector cache (Redis or in-process) and the owner workspace used as its scope.
        self.cache = None
        self.workspace_of = None
        self.closed = False
        self.on_indexed = None
        # E2 worker mode: a PipelineClient (set by the app). Jobs are then accepted durably in
        # PostgreSQL and executed by Celery workers; this process runs no indexing work itself.
        self.pipeline = None
        self.worker_mode = settings.platform_enabled and settings.pipeline_mode == "worker"

    async def start(self):
        if self.owner is not None:
            self.owner.acquire()
        try:
            if self.worker_mode:
                # Jobs outlive the API in worker mode: never mark them interrupted, never touch
                # worker scratch. Interrupted deletions are finished by the coordination sweeper
                # (any instance), once no reader holds a lease.
                return
            self.registry.recover()
            for path in (self.registry.root / ".uploads").glob("*.part"):
                path.unlink(missing_ok=True)
            # Resume only filesystem deletion, never automatically restart model work.
            with self.registry.db.connect() as conn:
                deleting = [r[0] for r in conn.execute("SELECT video_id FROM videos WHERE status='deleting'")]
            for video_id in deleting:
                self.coordination.tombstone(video_id)
                self.reclaim(video_id)
            # Only transient audio is removed; published/cached transcript tables survive.
            for row in self.registry.list(limit=1000000)["items"]:
                work = self.registry.asset(row["video_id"], "cache/transcript-work")
                if work.exists():
                    shutil.rmtree(work)
        except BaseException:
            if self.owner is not None:
                self.owner.release()
            raise

    async def close(self):
        self.closed = True
        for task in list(self.tasks.values()):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        if self.owner is not None:
            self.owner.release()

    @property
    def leases(self):
        """Leases held by this process, per video (diagnostics and tests; the coordinator is the truth)."""
        return self.coordination.local_counts()

    def _usable_video(self, video_id):
        row = self.registry.get(video_id)
        if row['trashed_at'] is not None:
            raise DomainError(409, 'video_trashed', 'Restore the video before using it')
        if row["status"] in {"deleting", "delete_failed"}:
            raise DomainError(409, "video_deleting", "Video is being deleted")
        return row

    def _retain(self, video_id, generations=None, purpose='read'):
        """A read lease on the video and the generations it names (default: the currently published ones).
        Refused once the video is tombstoned. The returned lease is callable to release it."""
        if generations is None:
            row = self.registry.get(video_id)
            generations = (row['active_generation'], row['transcript_generation'])
        return self.coordination.acquire(video_id, generations, purpose)

    def _restore(self, video_id, relative, *, prefix=False):
        if self.publications is not None:
            self.publications.ensure(video_id, relative, prefix=prefix)

    def cache_scope(self, video_id):
        """Authorization scope of cache keys: the owning workspace (platform) or this local registry."""
        workspace = self.workspace_of(video_id) if self.workspace_of is not None else None
        return workspace or 'local'

    def snapshot(self, row) -> ReadSnapshot:
        if row['transcript_generation']:
            self._restore(row['video_id'], f"memory/{row['transcript_generation']}.sqlite3")
        state = self.registry.transcript_state(row) if row['transcript_generation'] else {}
        return ReadSnapshot(row['video_id'], row['active_generation'], row['transcript_generation'],
                            state.get('text_version'), state.get('search_version'),
                            embedding_fingerprint(self.backend or get_backend()))

    async def _pin(self, video_id, purpose):
        """Usable-check, snapshot and lease, in that order; the lease names the snapshot's generations."""
        row = self._usable_video(video_id)
        snapshot = await run_sync(self.snapshot, row)
        lease = await run_sync(self.coordination.acquire, video_id,
                               (snapshot.visual_generation, snapshot.transcript_generation), purpose)
        return row, snapshot, lease

    def _reader(self, video_id, generation):
        """Shared read-only handle of a published generation (opened once per process, off the event loop)."""
        if not re.fullmatch(r"[0-9a-f]{32}", generation or ""):
            raise DomainError(409, "invalid_index_version", "Invalid stored index version")
        backend = self.backend or get_backend()
        key = (video_id, generation, embedding_fingerprint(backend))

        def open_reader():
            self._restore(video_id, f"memory/{generation}.sqlite3")
            self._restore(video_id, f"cache/{generation}", prefix=True)
            path = self.registry.asset(video_id, f"memory/{generation}.sqlite3")
            try:
                store = FrameStore(backend=backend, persist_path=str(path), read_only=True,
                                   derived_path=str(self.registry.asset(video_id, f"memory/{generation}.derived.sqlite3")))
            except RuntimeError as exc:
                raise DomainError(409, "incompatible_index", "Index model/schema is incompatible; back up and rebuild this video index") from exc
            store.asset_root = str(self.registry.asset(video_id, f"cache/{generation}"))
            if self.cache is not None:   # one reader per video, so one owner workspace per reader
                store.query_cache = (self.cache, self.cache_scope(video_id), self.settings.cache_ttl_s)
            return store
        return self.readers.get(key, open_reader)

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

    def schedule(self, video_id, *, kind="video", allow_retained=False, request_id=None):
        if self.closed:
            raise DomainError(503, "server_stopping", "Server is stopping")
        self._usable_video(video_id)
        if self.pipeline is not None:
            # Published indexes are immutable generations swapped by the worker's publish stage,
            # so readers holding a lease do not block accepting a new job.
            return self.pipeline.submit(video_id, kind=kind, request_id=request_id)
        if self.coordination.active(video_id) and not allow_retained:
            raise DomainError(409, "video_busy", "Video is in use; retry indexing later")
        job_id = self.registry.create_job(video_id, self.settings.max_queued_jobs, kind)
        task = asyncio.create_task(self._run(video_id, job_id))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))
        return job_id

    def _transcript_store(self, video_id, candidate=None):
        row = self.registry.get(video_id)
        generation = row["transcript_generation"] or candidate or row["active_generation"]
        if not generation or not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise DomainError(409, "transcript_not_ready", "Transcript indexing has not started")
        if row["transcript_generation"]:
            self._restore(video_id, f"memory/{generation}.sqlite3")
        path = self.registry.asset(video_id, f"memory/{generation}.sqlite3")
        store = TranscriptStore(path, video_id)
        if not row["transcript_generation"]:
            self.registry.update_video(video_id, transcript_generation=generation)
        return store

    async def _run_transcript(self, video_id, job_id, source, candidate):
        store = self._transcript_store(video_id, candidate)
        self.registry.update_video(video_id, transcript_status="running", transcript_error=None)
        try:
            row = self.registry.get(video_id)
            digest = row["source_sha256"] or await run_sync(checksum, source)
            if not row["source_sha256"]:
                self.registry.update_video(video_id, source_sha256=digest)
            def progress(phase, current, total):
                self.progress.progress(job_id, phase=phase, current=current, total=total)
            result = await index_transcript(source=source, source_sha256=digest, store=store, settings=self.settings,
                                            transcriber=self.transcriber, backend=self.backend or get_backend(),
                                            progress=progress, work_root=self.registry.asset(video_id, "cache/transcript-work"))
            self.registry.update_video(video_id, transcript_status=result["status"], transcript_error=result.get("error"))
            return result
        except asyncio.CancelledError:
            self.registry.update_video(video_id, transcript_status="interrupted", transcript_error="Transcript job interrupted")
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, ASRError) else "transcript_failed"
            message = str(exc) if isinstance(exc, ASRError) else "Transcript indexing failed; previous published text/index is retained"
            self.registry.update_video(video_id, transcript_status="failed", transcript_error=message)
            return {"status": "failed", "error_code": code, "error": message, **store.state()}

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

    async def _run_vision(self, video_id, job_id, source):
        self.registry.update_job(job_id, status="running", phase="playback")
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
        if job["phase"] == "vision_failed":
            raise RuntimeError(job["error"] or "index failed")
        result = job["result"] or {}
        if result.get("count", 0) <= 0 or not await store.list_frames():
            raise RuntimeError("index contains no usable frames")
        partial = bool(result.get("truncated") or result.get("filter_report", {}).get("embed_failures", {}).get("count"))
        result["model"] = {"fingerprint": store.model_fingerprint(), "dimension": store._dim}
        return store, result, partial

    async def _run(self, video_id, job_id):
        logger.info("index_queued video_id=%s job_id=%s", video_id, job_id)
        try:
            async with self.semaphore:
                row = self.registry.get(video_id)
                kind = self.registry.job(job_id)["kind"]
                self.registry.update_video(video_id, status="running", last_error=None)
                self.registry.update_job(job_id, status="running", phase="starting")
                source = self.registry.asset(video_id, row["source_path"])
                vision_store, result, vision_partial = None, {}, False
                vision = {"status": "not_requested"}
                if kind == "video":
                    try:
                        vision_store, result, vision_partial = await self._run_vision(video_id, job_id, source)
                        vision = {"status": "partial" if vision_partial else "ready"}
                    except Exception:
                        vision = {"status": "failed", "error_code": "index_failed", "error": "Visual indexing failed; retry is available"}
                        self.registry.update_job(job_id, status="running", phase="vision_failed")
                # A transcript-only retry can run without a usable visual index. On
                # initial ingestion both adapters share the same generation DB.
                candidate = (row["active_generation"] or job_id) if kind == "transcript" else job_id
                transcript = await self._run_transcript(video_id, job_id, source, candidate)
                result.update(video_id=video_id, stages={"vision": vision, "transcript": transcript})
                if kind == "transcript":
                    status = "failed" if transcript["status"] == "failed" else ("partial" if transcript["status"] == "partial" else "succeeded")
                elif vision_store:
                    status = "partial" if vision_partial or transcript["status"] in {"failed", "partial"} else "succeeded"
                else:
                    status = "partial" if transcript["status"] in {"ready", "partial"} else "failed"
                error = transcript if transcript["status"] in {"failed", "partial"} else (vision if vision["status"] == "failed" else {})
                published_row = self.registry.get(video_id)
                previous = published_row["active_generation"]
                if vision_store:
                    published_row["active_generation"] = job_id
                result["capabilities"] = self.registry.public(published_row)["capabilities"]
                self.registry.finish(video_id, job_id, status=status, result=result,
                                     error_code=error.get("error_code"), error=error.get("error"),
                                     vision_generation=job_id if vision_store else None,
                                     fingerprint=vision_store.model_fingerprint() if vision_store else None)
                if vision_store and previous and previous != job_id:
                    # Older requests keep reading the replaced generation; it is removed after the grace
                    # period once no lease names it (transcript tables inside it keep it alive, see sweep).
                    self.coordination.retire(video_id, previous)
                logger.info("index_complete video_id=%s job_id=%s status=%s", video_id, job_id, status)
                if self.on_indexed and status in {'succeeded', 'partial'}:
                    self.on_indexed(video_id)
        except asyncio.CancelledError:
            self.registry.update_job(job_id, status="interrupted", phase="interrupted",
                                     error_code="server_shutdown", error="Server stopped before indexing finished")
            self.registry.update_video(video_id, status="interrupted", last_error="Index interrupted; retry is available")
            raise
        except Exception as exc:
            logger.warning("index_failed video_id=%s job_id=%s error_type=%s", video_id, job_id, type(exc).__name__)
            self.registry.update_job(job_id, status="failed", phase="failed", error_code="index_failed", error="Indexing failed; retry is available")
            self.registry.update_video(video_id, status="failed", last_error="Indexing failed; see job details and retry")
        finally:
            row = self.registry.get(video_id)
            # A failed visual generation may still own a usable/cached transcript.
            if job_id not in {row["active_generation"], row["transcript_generation"]}:
                path = self.registry.asset(video_id, f"memory/{job_id}.sqlite3")
                for suffix in ("", "-wal", "-shm", ".lock"):
                    Path(str(path) + suffix).unlink(missing_ok=True)
                assets = self.registry.asset(video_id, f"cache/{job_id}")
                if assets.exists():
                    shutil.rmtree(assets)

    @asynccontextmanager
    async def transcript_context(self, video_id):
        row = self._usable_video(video_id)
        if not row["transcript_generation"]:
            raise DomainError(409, "transcript_not_ready", "No transcript job has initialized this video")
        lease = await run_sync(self.coordination.acquire, video_id, (row["transcript_generation"],), "transcript")
        try:
            yield self._transcript_store(video_id)
        finally:
            await run_sync(lease.release)

    @asynccontextmanager
    async def retrieval_context(self, video_id):
        """Pinned snapshot + read lease, no per-video lock (E3). Concurrent questions on one video read the
        same immutable generation in parallel; summaries written meanwhile go to the derived-summary file."""
        from vam.retrieval.pipeline import SearchContext
        row, snapshot, lease = await self._pin(video_id, "retrieval")
        try:
            visual, transcript, warnings = None, None, []
            if snapshot.visual_generation:
                try:
                    visual = await run_sync(self._reader, video_id, snapshot.visual_generation)
                except DomainError as exc:
                    warnings.append({'code': exc.code, 'source': 'vision'})
            if snapshot.transcript_generation:
                transcript = TranscriptStore(self.registry.asset(video_id, f"memory/{snapshot.transcript_generation}.sqlite3"), video_id)
            if visual is None and not snapshot.transcript_search_version:
                raise DomainError(409, 'index_not_ready', 'Video has no compatible searchable visual/transcript index')
            scope = await run_sync(self.cache_scope, video_id) if self.cache is not None else None
            yield SearchContext(row=row, visual=visual, transcript=transcript, backend=self.backend or get_backend(),
                                settings=self.settings, reranker=self.reranker, warnings=warnings, snapshot=snapshot,
                                cache=self.cache, cache_scope=scope)
        finally:
            await run_sync(lease.release)

    @asynccontextmanager
    async def memory_context(self, video_id):
        row = self._usable_video(video_id)
        generation = row["active_generation"]
        if not generation:
            raise DomainError(409, "index_not_ready", "Video has no usable visual index")
        lease = await run_sync(self.coordination.acquire, video_id, (generation,), "memory")
        try:
            yield row, await run_sync(self._reader, video_id, generation)
        finally:
            await run_sync(lease.release)

    def playback_asset(self, video_id):
        row = self._usable_video(video_id)
        if not row["playback_path"]:
            raise DomainError(409, "playback_not_ready", "Video playback is not ready")
        self._restore(video_id, row["playback_path"])
        path = self.registry.asset(video_id, row["playback_path"])
        if not path.is_file():
            raise DomainError(404, "asset_missing", "Video asset is missing")
        return path, self.coordination.acquire(video_id, (), "playback")

    def delete(self, video_id, requested_by=None):
        """Tombstone first (no new reader or job can start), then reclaim once no reader holds a lease.

        Returns ``{'status': 'deleted'}`` when reclaimed now, or ``{'status': 'deleting', ...}`` when existing
        readers must finish first; the coordination sweeper of any instance completes it (bounded by the
        lease TTL / maximum lifetime). Work that *writes* state tied to the video -- indexing jobs, a running
        question (conversation rows) or notes job -- is not interrupted: deletion is refused until it ends,
        as before. Pure readers (search, playback, frames, exports) are what the tombstone drains."""
        row = self.registry.get(video_id)
        if row["status"] not in {"deleting", "delete_failed"}:
            if self.pipeline is not None and self.pipeline.has_active(video_id):
                raise DomainError(409, "index_running", "Wait for indexing to finish before deleting")
            # Checks and the 'deleting' mark in one registry write transaction, serialized with job,
            # question and notes acceptance (which re-check the status in theirs): nothing that writes
            # state tied to the video can be accepted between the check and the mark.
            with self.registry.db.connect(write=True) as conn:
                if conn.execute("SELECT 1 FROM index_jobs WHERE video_id=? AND status IN ('queued','running')", (video_id,)).fetchone():
                    raise DomainError(409, "index_running", "Wait for indexing to finish before deleting")
                if conn.execute("SELECT 1 FROM conversation_requests r JOIN conversations c USING(conversation_id) "
                                "WHERE c.video_id=? AND r.status IN ('accepted','running')", (video_id,)).fetchone() or \
                        conn.execute("SELECT 1 FROM study_jobs WHERE video_id=? AND status IN ('queued','running')", (video_id,)).fetchone():
                    raise DomainError(409, "video_busy", "Wait for the running question or notes job to finish")
                conn.execute("UPDATE videos SET status='deleting' WHERE video_id=?", (video_id,))
            try:
                readers = self.coordination.tombstone(video_id, requested_by)
            except DomainError:   # a pipeline job was accepted meanwhile: deletion is refused, nothing changed
                self.registry.update_video(video_id, status=row["status"])
                raise
        else:
            readers = self.coordination.tombstone(video_id, requested_by)
        if readers:
            return {"video_id": video_id, "status": "deleting", "waiting_for_readers": readers}
        return self.reclaim(video_id)

    def reclaim(self, video_id):
        """Remove files and the registry row of a tombstoned video; idempotent (sweeper and API may race)."""
        directory = self.registry.directory(video_id)
        if self.queue_objects is not None:
            # Queue the published objects before their manifest rows go away with the video row (a crash
            # in between leaves queued deletions of tombstoned objects, never unreferenced leftovers).
            # The original (listed when it is also the playback file) is released with the video's ownership.
            with self.registry.db.connect() as conn:
                keys = [r[0] for r in conn.execute(
                    "SELECT object_key FROM publication_objects o JOIN videos v USING(video_id) "
                    "WHERE o.video_id=? AND o.relative_path<>v.source_path", (video_id,))]
            self.queue_objects(video_id, keys)
        try:
            if directory.exists():
                shutil.rmtree(directory)
            with self.registry.db.connect(write=True) as conn:
                conn.execute("DELETE FROM videos WHERE video_id=?", (video_id,))
        except OSError as exc:
            self.registry.update_video(video_id, status="delete_failed", last_error="Deletion incomplete; retry deletion")
            self.coordination.record_reclaim_failure(video_id, type(exc).__name__)
            raise DomainError(500, "delete_failed", "Video deletion incomplete; retry deletion") from exc
        self.readers.evict(video_id)
        if self.on_reclaimed is not None:
            self.on_reclaimed(video_id)
        self.coordination.mark_reclaimed(video_id)
        return {"video_id": video_id, "status": "deleted"}

    def remove_generation(self, video_id, generation):
        """Delete the files of a retired generation, unless it is (again) referenced by the video row."""
        try:
            row = self.registry.get(video_id)
        except DomainError:
            row = None
        if row is not None and generation in {row["active_generation"], row["transcript_generation"]}:
            self.coordination.mark_removed(video_id, generation)   # still published: never delete
            return False
        path = self.registry.asset(video_id, f"memory/{generation}.sqlite3")
        for name in (str(path), str(path)[:-len(".sqlite3")] + ".derived.sqlite3"):
            for suffix in ("", "-wal", "-shm", ".lock"):
                Path(name + suffix).unlink(missing_ok=True)
        assets = self.registry.asset(video_id, f"cache/{generation}")
        if assets.exists():
            shutil.rmtree(assets, ignore_errors=True)
        self.readers.evict(video_id, generation)
        self.coordination.mark_removed(video_id, generation)
        return True

    def sweep(self):
        """One coordination pass (any instance, idempotent): finish deletions whose readers are gone, and
        remove retired generations past their grace period that no lease names."""
        done = {"reclaimed": 0, "generations_removed": 0}
        for video_id in self.coordination.reclaimable():
            try:
                self.reclaim(video_id)
                done["reclaimed"] += 1
            except DomainError:
                logger.warning("reclaim_failed video_id=%s", video_id)
        for video_id, generation in self.coordination.due_retired(self.settings.generation_retire_grace_s):
            if self.remove_generation(video_id, generation):
                done["generations_removed"] += 1
        return done

    def trash(self, video_id, *, restore=False):
        row = self.registry.get(video_id)
        if self.coordination.active(video_id):
            raise DomainError(409, 'video_busy', 'Wait for the current operation to finish')
        with self.registry.db.connect() as conn:
            active = conn.execute("SELECT 1 FROM index_jobs WHERE video_id=? AND status IN ('queued','running')", (video_id,)).fetchone()
        if self.pipeline is not None and self.pipeline.has_active(video_id):
            active = True
        if active or row['status'] in {'deleting', 'delete_failed'}:
            raise DomainError(409, 'video_busy', 'Wait for processing or deletion to finish')
        self.registry.update_video(video_id, trashed_at=None if restore else time.time())
        return self.registry.public(self.registry.get(video_id))
