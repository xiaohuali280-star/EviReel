"""Read leases, deletion tombstones and retired generations (E3).

Replaces the per-process ``IndexingService.leases`` counters. Two implementations share one interface:

* ``LocalCoordinator`` -- single API process (single-user mode, or platform mode without
  ``API_MULTI_INSTANCE``). Same semantics, in memory.
* ``PgCoordinator`` -- any number of API instances. Leases are PostgreSQL rows with an expiry; the
  holder renews them every ``ttl/3`` while it reads, up to ``READ_LEASE_MAX_S``. A crashed instance
  stops renewing, so its leases expire after at most one TTL. A reader that outlives the maximum
  lifetime loses protection on purpose (its request deadline has passed; deletion must not wait forever).

Deletion protocol: ``tombstone()`` runs under the video's advisory lock, the same lock ``acquire()`` takes
before checking the tombstone, so after the tombstone commits no new lease can be granted. Reclamation
proceeds only once no unexpired lease remains.

Generation retirement: a publication that replaces a generation records it with ``retire()``. Its files
are removed only after ``GENERATION_RETIRE_GRACE_S`` and once no unexpired lease names that generation,
so an older request keeps reading the version it pinned.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field

from vam.video_registry import DomainError

logger = logging.getLogger(__name__)


def instance_id(settings) -> str:
    return settings.instance_id or f'{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}'


def redis_client(settings):
    """Shared Redis connection (rate windows, slots, cache, Pub/Sub); None when REDIS_URL is unset."""
    if not settings.redis_url:
        return None
    try:
        import redis
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError('REDIS_URL requires: pip install -e ".[platform]"') from exc
    return redis.Redis.from_url(settings.redis_url, socket_timeout=settings.redis_timeout_s,
                                socket_connect_timeout=settings.redis_timeout_s, health_check_interval=30)


@dataclass
class Lease:
    lease_id: str
    video_id: str
    generations: tuple
    purpose: str
    acquired: float = field(default_factory=time.monotonic)
    _release: object = None
    released: bool = False

    def release(self):
        if not self.released:
            self.released = True
            self._release(self)

    __call__ = release   # drop-in for the pre-E3 ``release()`` callables


class LocalCoordinator:
    """In-process implementation (one API instance)."""
    shared = False

    def __init__(self, settings):
        self.settings = settings
        self.leases: dict[str, Lease] = {}
        self.tombstones: set[str] = set()
        self.retired: dict[tuple, float] = {}
        self.lock = threading.Lock()

    def acquire(self, video_id, generations=(), purpose='read') -> Lease:
        with self.lock:
            if video_id in self.tombstones:
                raise DomainError(409, 'video_deleting', 'Video is being deleted')
            lease = Lease(uuid.uuid4().hex, video_id, tuple(g for g in generations if g), purpose, _release=self._drop)
            self.leases[lease.lease_id] = lease
            return lease

    def _drop(self, lease):
        with self.lock:
            self.leases.pop(lease.lease_id, None)

    def active(self, video_id, generation=None) -> int:
        with self.lock:
            return sum(1 for l in self.leases.values()
                       if l.video_id == video_id and (generation is None or generation in l.generations))

    def local_counts(self) -> dict:
        with self.lock:
            counts = {}
            for lease in self.leases.values():
                counts[lease.video_id] = counts.get(lease.video_id, 0) + 1
            return counts

    def tombstone(self, video_id, requested_by=None) -> int:
        with self.lock:
            self.tombstones.add(video_id)
        return self.active(video_id)

    def is_tombstoned(self, video_id) -> bool:
        return video_id in self.tombstones

    def clear_tombstone(self, video_id):
        with self.lock:
            self.tombstones.discard(video_id)

    def reclaimable(self, limit=20):
        return [v for v in list(self.tombstones) if not self.active(v)][:limit]

    def mark_reclaimed(self, video_id):
        self.clear_tombstone(video_id)

    def record_reclaim_failure(self, video_id, error):
        pass

    def retire(self, video_id, generation):
        if generation:
            with self.lock:
                self.retired.setdefault((video_id, generation), time.time())

    def due_retired(self, grace_s, limit=50):
        now = time.time()
        with self.lock:
            due = [key for key, at in self.retired.items() if now - at >= grace_s]
        return [key for key in due if not self.active(*key)][:limit]

    def mark_removed(self, video_id, generation):
        with self.lock:
            self.retired.pop((video_id, generation), None)

    def is_retired(self, video_id, generation) -> bool:
        return (video_id, generation) in self.retired

    async def start(self):
        pass

    async def close(self):
        pass


class PgCoordinator:
    """PostgreSQL implementation shared by every API instance."""
    shared = True

    def __init__(self, db, settings, holder):
        self.db, self.settings, self.holder = db, settings, holder
        self.ttl = settings.read_lease_ttl_s
        self.max_life = settings.read_lease_max_s
        self.held: dict[str, Lease] = {}
        self.lock = threading.Lock()
        self.renewer = None

    @staticmethod
    def _video_lock(conn, video_id):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 7032))", (video_id,))

    def acquire(self, video_id, generations=(), purpose='read') -> Lease:
        lease = Lease(str(uuid.uuid4()), video_id, tuple(g for g in generations if g), purpose, _release=self._drop)
        with self.db.transaction() as conn:
            self._video_lock(conn, video_id)
            if conn.execute("SELECT 1 FROM video_tombstones WHERE video_id=%s AND reclaimed_at IS NULL",
                            (video_id,)).fetchone():
                raise DomainError(409, 'video_deleting', 'Video is being deleted')
            conn.execute("INSERT INTO read_leases(lease_id, video_id, generations, holder, purpose, expires_at) "
                         "VALUES (%s,%s,%s,%s,%s, now() + make_interval(secs => %s))",
                         (lease.lease_id, video_id, list(lease.generations), self.holder, purpose, self.ttl))
        with self.lock:
            self.held[lease.lease_id] = lease
        return lease

    def local_counts(self) -> dict:
        with self.lock:
            counts = {}
            for lease in self.held.values():
                counts[lease.video_id] = counts.get(lease.video_id, 0) + 1
            return counts

    def _drop(self, lease):
        with self.lock:
            self.held.pop(lease.lease_id, None)
        try:
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM read_leases WHERE lease_id=%s", (lease.lease_id,))
        except Exception:   # the row expires on its own; never fail a finished request over cleanup
            logger.warning('read_lease_release_failed lease=%s', lease.lease_id)

    def renew_once(self):
        """Extend every lease this instance still holds and that is within its maximum lifetime."""
        now = time.monotonic()
        with self.lock:
            renew = [l.lease_id for l in self.held.values() if now - l.acquired < self.max_life]
            expired = [l.lease_id for l in self.held.values() if now - l.acquired >= self.max_life]
            for lease_id in expired:      # stop renewing: deletion may proceed after the row expires
                self.held.pop(lease_id, None)
        if expired:
            logger.warning('read_lease_max_lifetime_exceeded count=%s', len(expired))
        if renew:
            with self.db.transaction() as conn:
                conn.execute("UPDATE read_leases SET expires_at = now() + make_interval(secs => %s) "
                             "WHERE lease_id = ANY(%s::uuid[]) AND holder=%s", (self.ttl, renew, self.holder))
        return len(renew)

    def active(self, video_id, generation=None) -> int:
        with self.db.connect() as conn:
            if generation is None:
                row = conn.execute("SELECT count(*) AS n FROM read_leases WHERE video_id=%s AND expires_at > now()",
                                   (video_id,)).fetchone()
            else:
                row = conn.execute("SELECT count(*) AS n FROM read_leases WHERE video_id=%s AND expires_at > now() "
                                   "AND %s = ANY(generations)", (video_id, generation)).fetchone()
        return int(row['n'])

    def tombstone(self, video_id, requested_by=None) -> int:
        with self.db.transaction() as conn:
            self._video_lock(conn, video_id)
            # Pipeline jobs are inserted under the same lock after checking the tombstone
            # (JobRepository._insert_job): either the job exists now and deletion is refused, or the
            # job will be refused. Export jobs only read and are drained like any reader.
            if conn.execute("SELECT 1 FROM jobs WHERE video_id=%s AND status IN ('queued','running') "
                            "AND kind <> 'export'", (video_id,)).fetchone():
                raise DomainError(409, 'index_running', 'Wait for indexing to finish before deleting')
            conn.execute("INSERT INTO video_tombstones(video_id, requested_by) VALUES (%s,%s) "
                         "ON CONFLICT (video_id) DO UPDATE SET reclaimed_at = NULL", (video_id, requested_by))
            row = conn.execute("SELECT count(*) AS n FROM read_leases WHERE video_id=%s AND expires_at > now()",
                               (video_id,)).fetchone()
        return int(row['n'])

    def is_tombstoned(self, video_id) -> bool:
        with self.db.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM video_tombstones WHERE video_id=%s AND reclaimed_at IS NULL",
                                     (video_id,)).fetchone())

    def clear_tombstone(self, video_id):
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM video_tombstones WHERE video_id=%s", (video_id,))

    def reclaimable(self, limit=20):
        with self.db.connect() as conn:
            return [r['video_id'] for r in conn.execute(
                "SELECT t.video_id FROM video_tombstones t WHERE t.reclaimed_at IS NULL AND NOT EXISTS "
                "(SELECT 1 FROM read_leases l WHERE l.video_id = t.video_id AND l.expires_at > now()) "
                "ORDER BY t.requested_at LIMIT %s", (limit,))]

    def mark_reclaimed(self, video_id):
        with self.db.transaction() as conn:
            conn.execute("UPDATE video_tombstones SET reclaimed_at=now() WHERE video_id=%s", (video_id,))
            conn.execute("DELETE FROM read_leases WHERE video_id=%s", (video_id,))
            conn.execute("DELETE FROM retired_generations WHERE video_id=%s", (video_id,))

    def record_reclaim_failure(self, video_id, error):
        with self.db.transaction() as conn:
            conn.execute("UPDATE video_tombstones SET attempts = attempts + 1, last_error=%s WHERE video_id=%s",
                         (str(error)[:300], video_id))

    def retire(self, video_id, generation):
        if generation:
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO retired_generations(video_id, generation) VALUES (%s,%s) "
                             "ON CONFLICT DO NOTHING", (video_id, generation))

    def due_retired(self, grace_s, limit=50):
        with self.db.connect() as conn:
            return [(r['video_id'], r['generation']) for r in conn.execute(
                "SELECT g.video_id, g.generation FROM retired_generations g WHERE g.removed_at IS NULL "
                "AND g.retired_at < now() - make_interval(secs => %s) AND NOT EXISTS "
                "(SELECT 1 FROM read_leases l WHERE l.video_id = g.video_id AND l.expires_at > now() "
                " AND g.generation = ANY(l.generations)) ORDER BY g.retired_at LIMIT %s", (grace_s, limit))]

    def mark_removed(self, video_id, generation):
        with self.db.transaction() as conn:
            conn.execute("UPDATE retired_generations SET removed_at=now() WHERE video_id=%s AND generation=%s",
                         (video_id, generation))

    def is_retired(self, video_id, generation) -> bool:
        with self.db.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM retired_generations WHERE video_id=%s AND generation=%s",
                                     (video_id, generation)).fetchone())

    def purge_expired(self, older_than_s=3600):
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM read_leases WHERE expires_at < now() - make_interval(secs => %s)", (older_than_s,))

    async def start(self):
        async def renew():
            from vam.runtime import run_sync
            while True:
                await asyncio.sleep(max(1.0, self.ttl / 3))
                try:
                    await run_sync(self.renew_once)
                except Exception:
                    logger.exception('read_lease_renew_failed')
        self.renewer = asyncio.create_task(renew())

    async def close(self):
        if self.renewer is not None:
            self.renewer.cancel()
            try:
                await self.renewer
            except asyncio.CancelledError:
                pass
        with self.lock:
            held = list(self.held.values())
        for lease in held:
            lease.release()
