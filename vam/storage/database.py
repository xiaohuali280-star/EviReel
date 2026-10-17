from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3

class Database:
    """Short-lived connections; schema migrations never replace application data."""
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("unsupported EviReel registry schema")
            if version < 1:
                sql = (Path(__file__).parent / "migrations/001_registry.sql").read_text(encoding="utf-8")
                conn.executescript("BEGIN IMMEDIATE;\n" + sql + "\nPRAGMA user_version=1; COMMIT;")
            if version < 2:
                sql = (Path(__file__).parent / "migrations/002_transcripts.sql").read_text(encoding="utf-8")
                conn.executescript("BEGIN IMMEDIATE;\n" + sql + "\nPRAGMA user_version=2; COMMIT;")

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
