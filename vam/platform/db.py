"""PostgreSQL access for platform metadata (identities, ownership, quota, uploads).

Short-lived connections and short transactions; never hold a transaction across
object-storage or media work. Callers in async code use ``vam.runtime.run_sync``.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import re

SCHEMA_VERSION = 7
MIGRATIONS = Path(__file__).parent / "migrations"


class PlatformDatabaseError(RuntimeError):
    pass


def _driver():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - environment guard
        raise PlatformDatabaseError('PostgreSQL support requires: pip install -e ".[platform]"') from exc
    return psycopg, dict_row


class ConnectionPool:
    """Reuses idle connections (E3): a new PostgreSQL connection costs tens of ms and a TCP port in
    TIME_WAIT; per-statement connections under concurrent requests exhausted client ports in tests.

    Semantics match ``with psycopg.connect() as conn``: commit on success, rollback on error. A connection
    goes back to the idle list only if it is healthy and outside a transaction; at most ``max_idle`` are
    kept. Fork-aware: a child process (Celery prefork) never reuses its parent's sockets."""

    def __init__(self, factory, *, max_idle=8, max_idle_s=60):
        import threading
        self.factory, self.max_idle, self.max_idle_s = factory, max_idle, max_idle_s
        self.idle, self.lock, self.pid = [], threading.Lock(), os.getpid()

    def _take(self):
        import time
        stale = []
        conn = None
        with self.lock:
            if self.pid != os.getpid():
                self.idle, self.pid = [], os.getpid()
            while self.idle:
                candidate, since = self.idle.pop()
                # Idle connections may have been closed by the server or a proxy; old ones are replaced.
                if candidate.closed or time.monotonic() - since > self.max_idle_s:
                    stale.append(candidate)
                    continue
                conn = candidate
                break
        for old in stale:
            old.close()
        return conn or self.factory()

    def _give(self, conn):
        from psycopg.pq import TransactionStatus
        if conn.closed or conn.broken or conn.info.transaction_status != TransactionStatus.IDLE:
            conn.close()
            return
        with self.lock:
            if self.pid == os.getpid() and len(self.idle) < self.max_idle:
                import time
                self.idle.append((conn, time.monotonic()))
                return
        conn.close()

    @contextmanager
    def connection(self):
        conn = self._take()
        try:
            yield conn
            if not conn.closed:
                conn.commit()
        except BaseException:
            if not conn.closed and not conn.broken:
                try:
                    conn.rollback()
                except Exception:
                    conn.close()
            raise
        finally:
            self._give(conn)


class PlatformDB:
    def __init__(self, url: str, *, connect_timeout: int = 5, max_idle: int = 8):
        if not url:
            raise PlatformDatabaseError("DATABASE_URL is empty")
        self.url = url
        self.connect_timeout = connect_timeout
        psycopg, dict_row = _driver()
        self.pool = ConnectionPool(lambda: psycopg.connect(self.url, row_factory=dict_row,
                                                           connect_timeout=self.connect_timeout), max_idle=max_idle)

    @contextmanager
    def connect(self):
        with self.pool.connection() as conn:
            yield conn

    @contextmanager
    def transaction(self):
        """One atomic unit of work; rolled back on any exception."""
        with self.connect() as conn:
            with conn.transaction():
                yield conn

    # -- schema -------------------------------------------------------------
    def current_version(self) -> int:
        with self.connect() as conn:
            exists = conn.execute("SELECT to_regclass('public.schema_migrations') AS t").fetchone()["t"]
            if not exists:
                return 0
            row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
            return int(row["v"])

    def pending_migrations(self):
        current = self.current_version()
        files = sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))
        return [(int(f.name[:3]), f) for f in files if int(f.name[:3]) > current]

    def migrate(self, *, dry_run: bool = False):
        pending = self.pending_migrations()
        if dry_run:
            return [f.name for _, f in pending]
        applied = []
        with self.transaction() as conn:
            # Serialize concurrent migrators; the advisory lock ends with the transaction.
            conn.execute("SELECT pg_advisory_xact_lock(4207031)")
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, "
                         "name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
            done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
            for version, path in pending:
                if version in done:
                    continue
                for statement in _statements(path.read_text(encoding="utf-8")):
                    conn.execute(statement)
                conn.execute("INSERT INTO schema_migrations(version, name) VALUES (%s, %s)", (version, path.name))
                applied.append(path.name)
        return applied

    def require_schema(self):
        version = self.current_version()
        if version < SCHEMA_VERSION:
            raise PlatformDatabaseError(
                f"Platform schema version {version} < {SCHEMA_VERSION}; run: python -m vam.platform.migrate schema")
        if version > SCHEMA_VERSION:
            raise PlatformDatabaseError(f"Platform schema version {version} is newer than this code ({SCHEMA_VERSION})")


def _statements(sql: str):
    """Split a migration file on statement terminators (migrations contain no ';' literals)."""
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def redact_url(url: str) -> str:
    """Hide credentials when a database URL must appear in logs or CLI output."""
    return re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", url or "")
