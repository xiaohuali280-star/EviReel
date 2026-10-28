"""PostgreSQL access for platform metadata (identities, ownership, quota, uploads).

Short-lived connections and short transactions; never hold a transaction across
object-storage or media work. Callers in async code use ``vam.runtime.run_sync``.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import re

SCHEMA_VERSION = 2
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


class PlatformDB:
    def __init__(self, url: str, *, connect_timeout: int = 5):
        if not url:
            raise PlatformDatabaseError("DATABASE_URL is empty")
        self.url = url
        self.connect_timeout = connect_timeout

    @contextmanager
    def connect(self):
        psycopg, dict_row = _driver()
        with psycopg.connect(self.url, row_factory=dict_row, connect_timeout=self.connect_timeout) as conn:
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
