"""E3: the registry read model in PostgreSQL behind the SQLite `Database` interface.

The registry code (VideoRegistry, ConversationStore, StudyService...) is written against short
`connect()` / `connect(write=True)` blocks with `?` parameters and sqlite3.Row-like rows. This backend
keeps that contract so the same code runs on either store:

- statements are translated once (`?` -> `%s`, `INSERT OR IGNORE` -> `ON CONFLICT DO NOTHING`, instr,
  json_each) and cached;
- rows support both `row[0]` and `row["column"]` and `dict(row)`, like sqlite3.Row;
- a write block holds a per-schema transaction-level advisory lock, so registry writers are serialized
  exactly like SQLite's BEGIN IMMEDIATE: the check-then-insert code (queue capacity, one active job or
  request) keeps its meaning across API instances and workers. Reads never take it.

    python -m vam.storage.pg_registry copy --data-root DATA_ROOT --database-url URL [--schema registry] [--to postgres|sqlite]
    python -m vam.storage.pg_registry verify --data-root DATA_ROOT --database-url URL [--schema registry]
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import re
from contextlib import contextmanager
from pathlib import Path

from vam.storage.database import _statements

MIGRATIONS = ("001_registry.sql",)
# FK order: parents first (copy), children first (clear).
TABLES = ("videos", "index_jobs", "conversations", "conversation_requests", "messages", "study_jobs",
          "study_artifacts", "publication_objects")


@functools.lru_cache(maxsize=1024)
def translate(sql: str) -> str:
    sql = sql.replace("%", "%%").replace("?", "%s")
    if "INSERT OR IGNORE" in sql:
        sql = sql.replace("INSERT OR IGNORE", "INSERT").rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    sql = re.sub(r"\binstr\(", "strpos(", sql)
    return sql.replace("SELECT value FROM json_each(%s)", "SELECT jsonb_array_elements_text(%s::jsonb)")


class Row(tuple):
    """sqlite3.Row semantics: index and column access, keys(), dict(row)."""

    def __new__(cls, names, values):
        row = super().__new__(cls, values)
        row._names = names
        return row

    def keys(self):
        return list(self._names)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._names.index(key))
            except ValueError:
                raise IndexError(f"No item with that key: {key}") from None
        return tuple.__getitem__(self, key)


def _row_factory(cursor):
    names = [column.name for column in (cursor.description or ())]
    return lambda values: Row(names, values)


class _Connection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        return self.raw.execute(translate(sql), tuple(params))


def schema_for(root, schema: str) -> str:
    """"auto": one schema per DATA_ROOT (isolated test runs); otherwise the configured name."""
    if schema != "auto":
        return schema
    return "registry_" + hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]


class PgRegistryDatabase:
    """Drop-in for vam.storage.database.Database (connect / connect(write=True))."""
    backend = "postgres"

    def __init__(self, url: str, schema: str, *, connect_timeout: int = 5):
        if not url:
            raise RuntimeError("REGISTRY_BACKEND=postgres needs REGISTRY_DATABASE_URL or DATABASE_URL")
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise ValueError("invalid registry schema name")
        import psycopg
        self._psycopg, self.url, self.schema, self.connect_timeout = psycopg, url, schema, connect_timeout
        self._lock_key = int.from_bytes(hashlib.sha256(schema.encode()).digest()[:8], "big", signed=True)
        from vam.platform.db import ConnectionPool
        self.pool = ConnectionPool(lambda: psycopg.connect(url, row_factory=_row_factory, connect_timeout=connect_timeout,
                                                           options=f"-c search_path={schema}"))
        self.migrate()

    def migrate(self):
        conn = self._psycopg.connect(self.url, connect_timeout=self.connect_timeout, autocommit=True)
        try:
            with conn.transaction():
                # Concurrent instances starting together apply each migration once (same idea as the
                # SQLite version re-check under BEGIN IMMEDIATE).
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_key,))
                conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
                conn.execute(f"SET LOCAL search_path TO {self.schema}")
                conn.execute("CREATE TABLE IF NOT EXISTS registry_version (version integer NOT NULL)")
                row = conn.execute("SELECT max(version) FROM registry_version").fetchone()
                current = row[0] or 0
                if current > len(MIGRATIONS):
                    raise RuntimeError("unsupported EviReel registry schema")
                for number, name in enumerate(MIGRATIONS, start=1):
                    if number <= current:
                        continue
                    sql = (Path(__file__).parent / "migrations_pg" / name).read_text(encoding="utf-8")
                    for statement in _statements(sql):
                        conn.execute(statement)
                    conn.execute("INSERT INTO registry_version VALUES (%s)", (number,))
        finally:
            conn.close()

    @contextmanager
    def connect(self, *, write=False):
        with self.pool.connection() as raw:
            if write:   # held until this block's commit/rollback
                raw.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_key,))
            yield _Connection(raw)


def registry_database(settings, root):
    """The registry store selected by settings (None = the SQLite file under DATA_ROOT)."""
    if getattr(settings, "registry_backend", "sqlite") != "postgres":
        return None
    return PgRegistryDatabase(settings.registry_database_url or settings.database_url,
                              schema_for(root, settings.registry_schema))


# ---------------------------------------------------------------------------------------------- copy
def _columns(conn, table):
    cursor = conn.execute(f"SELECT * FROM {table} LIMIT 0")
    return [d[0] for d in cursor.description]


def copy_registry(source, target):
    """Copy every registry table (in FK order) into an empty target store and verify row counts and
    per-table content digests. The source is only read. Returns {table: rows}."""
    counts = {}
    with source.connect() as src, target.connect(write=True) as dst:
        for table in TABLES:
            if dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                raise RuntimeError(f"target table {table} is not empty; refusing to merge")
        for table in TABLES:
            columns = _columns(src, table)
            rows = src.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
            insert = f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' * len(columns))})"
            for row in rows:
                dst.execute(insert, tuple(row))
            counts[table] = len(rows)
        if getattr(target, "backend", "sqlite") == "postgres":
            dst.execute("SELECT setval(pg_get_serial_sequence('messages','message_id'), "
                        "COALESCE((SELECT MAX(message_id) FROM messages), 0) + 1, false)")
    mismatch = verify_registry(source, target)
    if mismatch:
        raise RuntimeError(f"copy verification failed: {mismatch}")
    return counts


def _digest(conn, table):
    columns = sorted(_columns(conn, table))
    order = {"videos": "video_id", "index_jobs": "job_id", "conversations": "conversation_id",
             "conversation_requests": "conversation_id,request_id", "messages": "message_id",
             "study_jobs": "job_id", "study_artifacts": "artifact_id",
             "publication_objects": "video_id,relative_path"}[table]
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}"):
        digest.update(repr(tuple(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
                                 for v in row)).encode())
        count += 1
    return count, digest.hexdigest()


def verify_registry(source, target):
    """Tables whose row count or content digest differ (empty dict = identical)."""
    with source.connect() as src, target.connect() as dst:
        return {table: {"source": a, "target": b} for table in TABLES
                if (a := _digest(src, table)) != (b := _digest(dst, table))}


def main(argv=None):
    from vam.storage.database import Database
    parser = argparse.ArgumentParser(prog="python -m vam.storage.pg_registry")
    parser.add_argument("command", choices=["copy", "verify"])
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--schema", default="registry")
    parser.add_argument("--to", choices=["postgres", "sqlite"], default="postgres",
                        help="copy direction; 'sqlite' writes back into an empty SQLite registry (rollback)")
    args = parser.parse_args(argv)
    root = Path(args.data_root).expanduser().resolve()
    sqlite_path = root / "registry.sqlite3"
    if args.command == "copy" and args.to == "sqlite" and sqlite_path.exists():
        raise SystemExit(f"{sqlite_path} exists; move it aside before copying back")
    if args.command != "copy" or args.to == "postgres":
        if not sqlite_path.is_file():
            raise SystemExit(f"registry not found: {sqlite_path}")
    lite = Database(sqlite_path)
    pg = PgRegistryDatabase(args.database_url, schema_for(root, args.schema))
    if args.command == "verify":
        mismatch = verify_registry(lite, pg)
        print("identical" if not mismatch else mismatch)
        return 1 if mismatch else 0
    source, target = (lite, pg) if args.to == "postgres" else (pg, lite)
    for table, rows in copy_registry(source, target).items():
        print(f"{table}: {rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
