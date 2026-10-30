from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3

MIGRATIONS = ("001_registry.sql", "002_transcripts.sql", "003_conversations.sql", "004_study.sql",
              "005_publication_fence.sql", "006_conversation_owner.sql",
              "007_publication_objects.sql")


def _statements(sql):
    """Split a migration into complete statements (sqlite3.complete_statement respects quoted ';')."""
    buffer = ""
    for line in sql.splitlines(keepends=True):
        if not buffer and line.lstrip().startswith("--"):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip().strip(";").strip():
                yield buffer.strip()
            buffer = ""
    if buffer.strip():
        yield buffer.strip()


class Database:
    """Short-lived connections; schema migrations never replace application data."""
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            if conn.execute("PRAGMA user_version").fetchone()[0] > len(MIGRATIONS):
                raise RuntimeError("unsupported EviReel registry schema")
            for number, name in enumerate(MIGRATIONS, start=1):
                # The version is re-read inside the write transaction: API instances starting together on
                # one DATA_ROOT apply each migration exactly once (the loser sees the new version).
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if conn.execute("PRAGMA user_version").fetchone()[0] < number:
                        for statement in _statements((Path(__file__).parent / "migrations" / name).read_text(encoding="utf-8")):
                            conn.execute(statement)
                        conn.execute(f"PRAGMA user_version={number}")
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise

    @contextmanager
    def connect(self, *, write=False):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

class OwnerLock:
    """Reject multiple server workers before recovering persisted running tasks."""
    def __init__(self, root: Path):
        self.path = root / ".server.lock"
        self.handle = None

    def acquire(self):
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("DATA_ROOT already owned; run EviReel with one worker") from exc

    def release(self):
        if self.handle is None:
            return
        if os.name == "nt":
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
