"""E3: published files as immutable objects, local DATA_ROOT files as a rebuildable cache.

A publication (the worker's `project` stage) makes index generations, their frame assets and the
playback file visible. Before the publication pointer moves, every such file is stored in object storage
and listed in the registry's `publication_objects` manifest, in the same registry transaction as the
pointer (VideoRegistry.publish_fenced). An API instance or worker whose DATA_ROOT lacks a published
file (new volume, lost disk) downloads it from the manifest, verifies its sha256 and moves it into place
atomically; readers never see a partial file.

Keys are per publication (`published/<video>/<seq>/<path>`); an unchanged file (same sha256 as the
previous manifest row) keeps its existing key, so a large playback file is uploaded once. Keys that leave
the manifest are queued in `object_deletions` (reason 'superseded'); keys of a deleted video are queued
before its rows are removed (reason 'video_deleted').

Limit (E3): a present local file is trusted. Index generations are immutable, but the transcript store
file gains new versions in place; with one shared DATA_ROOT (the E3 deployment) every instance sees the
same file. Separate per-instance volumes would need a version check before reuse; not supported yet.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from vam.platform import cleanup

logger = logging.getLogger(__name__)


def _sha256(path) -> str:
    from vam.pipeline.context import sha256_file
    return sha256_file(path)


def _snapshot_sqlite(path: Path, target: Path):
    """Consistent single-file copy of a (possibly WAL-mode) SQLite database."""
    src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()


def published_paths(registry, video_id, *, vision_generation, transcript_generation, playback_path) -> list[str]:
    """Relative paths (under the video directory) that a publication makes visible."""
    paths = []
    for generation in (vision_generation, transcript_generation):
        if generation and registry.asset(video_id, f"memory/{generation}.sqlite3").is_file():
            paths.append(f"memory/{generation}.sqlite3")
    if vision_generation:
        assets = registry.asset(video_id, f"cache/{vision_generation}")
        if assets.is_dir():
            paths += sorted(p.relative_to(registry.directory(video_id)).as_posix() for p in assets.rglob("*") if p.is_file())
    if playback_path:
        # The playback file may be the original itself (already browser-playable): it is then listed with the
        # upload's object (no second copy), see store_publication.
        paths.append(playback_path)
    return list(dict.fromkeys(paths))


def manifest(registry, video_id) -> dict:
    with registry.db.connect() as conn:
        rows = conn.execute("SELECT * FROM publication_objects WHERE video_id=?", (video_id,)).fetchall()
    return {r["relative_path"]: dict(r) for r in rows}


def store_publication(ctx, job, fence, paths) -> list[dict]:
    """Upload what the manifest lacks (or what changed) and return the new manifest rows."""
    video_id = job["video_id"]
    previous = manifest(ctx.registry, video_id)
    source_path = ctx.registry.get(video_id)["source_path"]
    with ctx.db.connect() as conn:
        owned = conn.execute("SELECT object_key, sha256, size_bytes FROM video_ownership WHERE video_id=%s",
                             (video_id,)).fetchone()
    scratch = ctx.workdir(f"publish-{job['job_id']}")
    rows = []
    for relative in paths:
        if relative == source_path:
            if owned and owned["object_key"] and owned["sha256"]:   # the verified upload object (E1)
                rows.append({"relative_path": relative, "object_key": owned["object_key"], "sha256": owned["sha256"],
                             "size_bytes": int(owned["size_bytes"]), "source": True})
            continue
        local = ctx.registry.asset(video_id, relative)
        if not local.is_file():
            continue
        source = local
        if relative.endswith(".sqlite3"):
            source = scratch / (uuid.uuid4().hex + ".sqlite3")
            _snapshot_sqlite(local, source)
        sha = _sha256(source)
        old = previous.get(relative)
        if old and old["sha256"] == sha:
            key, size = old["object_key"], old["size_bytes"]
        else:
            key = f"{ctx.prefix}published/{video_id}/{fence}/{relative}"
            info = ctx.store.put_file(key, source)
            size = info.size
        rows.append({"relative_path": relative, "object_key": key, "sha256": sha, "size_bytes": size})
    ctx.drop_workdir(f"publish-{job['job_id']}")
    return rows


def queue_unreferenced(db, keys, reason, video_id):
    """Durably queue object deletions (PostgreSQL platform DB)."""
    keys = sorted(set(keys))
    if not keys:
        return
    with db.transaction() as conn:
        for key in keys:
            cleanup.enqueue(conn, key, reason, video_id=video_id)


class PublicationCache:
    """API/worker side: make sure a published file exists locally before it is opened."""

    def __init__(self, registry, store):
        self.registry, self.store = registry, store
        self.locks: dict[tuple, threading.Lock] = {}
        self.guard = threading.Lock()
        self.restored = 0

    def _lock(self, key):
        with self.guard:
            return self.locks.setdefault(key, threading.Lock())

    def ensure(self, video_id, relative, *, prefix=False) -> int:
        """Restore `relative` (a file, or with prefix=True every manifest file under it) when missing.
        Returns the number of files downloaded."""
        local = self.registry.asset(video_id, relative)
        if not prefix and local.is_file():
            return 0
        with self.registry.db.connect() as conn:
            if prefix:
                rows = conn.execute("SELECT * FROM publication_objects WHERE video_id=? AND substr(relative_path,1,?)=?",
                                    (video_id, len(relative) + 1, relative.rstrip("/") + "/")).fetchall()
            else:
                rows = conn.execute("SELECT * FROM publication_objects WHERE video_id=? AND relative_path=?",
                                    (video_id, relative)).fetchall()
        restored = 0
        for row in rows:
            restored += self._restore(video_id, dict(row))
        return restored

    def _restore(self, video_id, row) -> int:
        target = self.registry.asset(video_id, row["relative_path"])
        if target.is_file():
            return 0
        with self._lock((video_id, row["relative_path"])):
            if target.is_file():
                return 0
            import hashlib
            target.parent.mkdir(parents=True, exist_ok=True)
            # Short partial name in the video directory (same filesystem, so the final rename is atomic).
            partial = self.registry.directory(video_id) / f".restore-{uuid.uuid4().hex[:12]}.part"
            digest = hashlib.sha256()
            started = time.monotonic()
            try:
                with partial.open("wb") as handle:
                    for chunk in self.store.iter_object(row["object_key"]):
                        digest.update(chunk)
                        handle.write(chunk)
                if digest.hexdigest() != row["sha256"]:
                    raise RuntimeError(f"published object {row['object_key']} does not match its sha256")
                os.replace(partial, target)
            finally:
                partial.unlink(missing_ok=True)
            self.restored += 1
            logger.info("publication_restored video=%s path=%s bytes=%s ms=%.0f", video_id, row["relative_path"],
                        row["size_bytes"], (time.monotonic() - started) * 1000)
            return 1
