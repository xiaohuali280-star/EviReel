"""Read leases, deletion tombstones and retired generations (E3).

Replaces the per-process ``IndexingService.leases`` counters. Two implementations share one interface:

* ``LocalCoordinator`` -- single API process (single-user mode, or platform mode without
  ``API_MULTI_INSTANCE``). Same semantics, in memory.
* ``PgCoordinator`` -- any number of API instances and workers. Leases are PostgreSQL rows with an
  expiry; the holder renews them every ``ttl/3`` while it reads, up to ``READ_LEASE_MAX_S``. A crashed
  instance stops renewing, so its leases expire after at most one TTL.

One lock decides everything about a video's files: the per-video advisory lock (7032). Under it:

* ``acquire`` grants a lease only if the video is not tombstoned and no generation it names has been
  claimed for removal (``StaleSnapshot`` otherwise: the caller re-reads the video and pins again);
* ``claim_removal`` marks a retired generation removed only if no unexpired lease names it -- files are
  deleted after that commit, so a lease is either granted first (and blocks removal) or refused;
* ``claim_reclaim`` re-checks, right before a deleted video's files go, that no lease exists;
* ``tombstone`` refuses while a pipeline job is active (jobs are inserted under the same lock).

Lease loss: a shared lease is valid locally until ``deadline`` -- the start of its last successful write
plus the TTL minus a margin, so it always ends before the database row can expire -- and never beyond the
maximum lifetime. Renewal never revives a row that already expired or vanished. When a lease is lost
(maximum lifetime, renewals failing past the deadline, row gone) its ``on_lost`` callbacks run: readers
cancel the request, streamed responses stop sending. A lost lease's row is left to expire rather than
deleted, giving work already in flight the remaining TTL before the sweeper may remove anything.
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


class StaleSnapshot(DomainError):
    """A generation named by a new lease was already claimed for removal: pin a fresh snapshot."""
    def __init__(self):
        super().__init__(409, 'snapshot_stale', 'The pinned index version was replaced; retry')


def lease_lost_error():
    return DomainError(503, 'read_lease_lost', 'The read protection of this request expired; retry')


@dataclass
class Lease:
    lease_id: str
    video_id: str
    generations: tuple
    purpose: str
    acquired: float = field(default_factory=time.monotonic)
    _release: object = None
    released: bool = False
    deadline: float = float('inf')      # monotonic end of local validity (shared leases)
    lost: str | None = None             # reason, once lost
    _watchers: list = field(default_factory=list)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def release(self):
        if not self.released:
            self.released = True
            self._release(self)

    __call__ = release   # drop-in for the pre-E3 ``release()`` callables

    @property
    def valid(self) -> bool:
        return self.lost is None and time.monotonic() < self.deadline

    def check(self):
        if not self.valid:
            raise lease_lost_error()

    def on_lost(self, callback):
        """Run ``callback()`` once when this lease is lost (immediately if it already is)."""
        with self._guard:
            if self.lost is None:
                self._watchers.append(callback)
                return
        callback()

    def mark_lost(self, reason):
        with self._guard:
            if self.lost is not None:
                return
            self.lost, watchers, self._watchers = reason, self._watchers, []
        logger.warning('read_lease_lost lease=%s video=%s purpose=%s reason=%s', self.lease_id, self.video_id,
                       self.purpose, reason)
        for callback in watchers:
            try:
                callback()
            except Exception:
                logger.exception('read_lease_lost_callback_failed')


class LocalCoordinator:
    """In-process implementation (one API instance)."""
    shared = False

    def __init__(self, settings):
        self.settings = settings
        self.leases: dict[str, Lease] = {}
        self.tombstones: set[str] = set()
        self.retired: dict[tuple, float] = {}
        self.removed: set[tuple] = set()
        self.lock = threading.Lock()

    def acquire(self, video_id, generations=(), purpose='read') -> Lease:
        with self.lock:
            if video_id in self.tombstones:
                raise DomainError(409, 'video_deleting', 'Video is being deleted')
            if any((video_id, g) in self.removed for g in generations if g):
                raise StaleSnapshot()
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

    def claim_reclaim(self, video_id) -> bool:
        with self.lock:
            return video_id in self.tombstones and not any(l.video_id == video_id for l in self.leases.values())

    def mark_reclaimed(self, video_id):
        with self.lock:
            self.tombstones.discard(video_id)
            self.removed = {k for k in self.removed if k[0] != video_id}

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

    def claim_removal(self, video_id, generation) -> bool:
        with self.lock:
            if any(l.video_id == video_id and generation in l.generations for l in self.leases.values()):
                return False
            self.retired.pop((video_id, generation), None)
            self.removed.add((video_id, generation))
            return True

    def unretire(self, video_id, generation):
        with self.lock:
            self.retired.pop((video_id, generation), None)

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
        self.margin = self.ttl / 6       # local validity ends this long before the row can expire
        self.held: dict[str, Lease] = {}
        self.lock = threading.Lock()
        self.renewer = self.watcher = self.thread = None
        self.stopping = threading.Event()

    @staticmethod
    def _video_lock(conn, video_id):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 7032))", (video_id,))

    def acquire(self, video_id, generations=(), purpose='read') -> Lease:
        lease = Lease(str(uuid.uuid4()), video_id, tuple(g for g in generations if g), purpose, _release=self._drop)
        started = time.monotonic()
        with self.db.transaction() as conn:
            self._video_lock(conn, video_id)
            if conn.execute("SELECT 1 FROM video_tombstones WHERE video_id=%s AND reclaimed_at IS NULL",
                            (video_id,)).fetchone():
                raise DomainError(409, 'video_deleting', 'Video is being deleted')
            if lease.generations and conn.execute(
                    "SELECT 1 FROM retired_generations WHERE video_id=%s AND generation = ANY(%s) "
                    "AND removed_at IS NOT NULL", (video_id, list(lease.generations))).fetchone():
                raise StaleSnapshot()
            conn.execute("INSERT INTO read_leases(lease_id, video_id, generations, holder, purpose, expires_at) "
                         "VALUES (%s,%s,%s,%s,%s, now() + make_interval(secs => %s))",
                         (lease.lease_id, video_id, list(lease.generations), self.holder, purpose, self.ttl))
        # The row expires at now()+ttl as measured by the server *after* `started`: ending local validity at
        # started+ttl-margin keeps it strictly inside the row's protection.
        lease.deadline = min(started + self.ttl - self.margin, lease.acquired + self.max_life)
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
        if lease.lost is not None:
            return   # left to expire: in-flight work of a cancelled reader keeps the remaining TTL
        try:
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM read_leases WHERE lease_id=%s", (lease.lease_id,))
        except Exception:   # the row expires on its own; never fail a finished request over cleanup
            logger.warning('read_lease_release_failed lease=%s', lease.lease_id)

    def _lose(self, leases, reason):
        with self.lock:
            for lease in leases:
                self.held.pop(lease.lease_id, None)
        for lease in leases:
            lease.mark_lost(reason)

    def expire_overdue(self):
        """Local check (no database): leases past the maximum lifetime or their local deadline are lost."""
        now = time.monotonic()
        with self.lock:
            leases = list(self.held.values())
        self._lose([l for l in leases if now - l.acquired >= self.max_life], 'max_lifetime')
        self._lose([l for l in leases if l.lost is None and now >= l.deadline], 'not_renewed')

    def renew_once(self):
        """Extend the leases this instance holds. Only unexpired rows are extended (an expired row is never
        revived); a lease whose row was not extended is lost."""
        self.expire_overdue()
        with self.lock:
            leases = list(self.held.values())
        if not leases:
            return 0
        started = time.monotonic()
        with self.db.transaction() as conn:
            renewed = {str(r['lease_id']) for r in conn.execute(
                "UPDATE read_leases SET expires_at = now() + make_interval(secs => %s) "
                "WHERE lease_id = ANY(%s::uuid[]) AND holder=%s AND expires_at > now() RETURNING lease_id",
                (self.ttl, [l.lease_id for l in leases], self.holder))}
        for lease in leases:
            if lease.lease_id in renewed:
                lease.deadline = min(started + self.ttl - self.margin, lease.acquired + self.max_life)
        self._lose([l for l in leases if l.lease_id not in renewed and not l.released], 'row_expired')
        return len(renewed)

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

    def claim_reclaim(self, video_id) -> bool:
        """Right before a deleted video's files go: still tombstoned and no unexpired lease (video lock)."""
        with self.db.transaction() as conn:
            self._video_lock(conn, video_id)
            return bool(conn.execute(
                "SELECT 1 FROM video_tombstones t WHERE t.video_id=%s AND t.reclaimed_at IS NULL AND NOT EXISTS "
                "(SELECT 1 FROM read_leases l WHERE l.video_id=t.video_id AND l.expires_at > now())",
                (video_id,)).fetchone())

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

    def claim_removal(self, video_id, generation) -> bool:
        """Mark a retired generation removed if no unexpired lease names it. From this commit on no lease
        naming it is granted, so its files may be deleted."""
        with self.db.transaction() as conn:
            self._video_lock(conn, video_id)
            return bool(conn.execute(
                "UPDATE retired_generations g SET removed_at=now() WHERE g.video_id=%s AND g.generation=%s "
                "AND g.removed_at IS NULL AND NOT EXISTS (SELECT 1 FROM read_leases l WHERE l.video_id=g.video_id "
                "AND l.expires_at > now() AND g.generation = ANY(l.generations)) RETURNING 1",
                (video_id, generation)).fetchone())

    def unretire(self, video_id, generation):
        """A retired generation that is published again: it stays (never marked removed)."""
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM retired_generations WHERE video_id=%s AND generation=%s AND removed_at IS NULL",
                         (video_id, generation))

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

        async def watch():   # independent of the renewer: a hung database call must not delay the loss
            while True:
                await asyncio.sleep(0.5)
                self.expire_overdue()
        self.renewer = asyncio.create_task(renew())
        self.watcher = asyncio.create_task(watch())

    def start_thread(self):
        """Worker processes (no event loop): renew and watch from a daemon thread."""
        def loop():
            last = time.monotonic()
            while not self.stopping.wait(0.5):
                self.expire_overdue()
                if time.monotonic() - last >= max(1.0, self.ttl / 3):
                    last = time.monotonic()
                    try:
                        self.renew_once()
                    except Exception:
                        logger.exception('read_lease_renew_failed')
        if self.thread is None:
            self.thread = threading.Thread(target=loop, name='read-lease-renewer', daemon=True)
            self.thread.start()

    async def close(self):
        self.stopping.set()
        for task in (self.renewer, self.watcher):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        with self.lock:
            held = list(self.held.values())
        for lease in held:
            lease.release()
