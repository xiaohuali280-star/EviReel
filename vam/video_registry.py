from __future__ import annotations

from pathlib import Path
import json
import re
import time
import uuid

from vam.storage.database import Database

class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

class VideoRegistry:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "videos").mkdir(exist_ok=True)
        (self.root / ".uploads").mkdir(exist_ok=True)
        if (self.root / "videos").is_symlink() or (self.root / ".uploads").is_symlink():
            raise DomainError(400, "unsafe_path", "Storage subdirectories must not be symbolic links")
        self.db = Database(self.root / "registry.sqlite3")

    def directory(self, video_id):
        if not re.fullmatch(r"v_[0-9a-f]{32}", str(video_id)):
            raise DomainError(404, "video_not_found", "Video not found")
        directory = self.root / "videos" / video_id
        if directory.is_symlink() or directory.resolve().parent != (self.root / "videos").resolve():
            raise DomainError(400, "unsafe_path", "Invalid video asset directory")
        return directory

    def asset(self, video_id, relative):
        directory = self.directory(video_id)
        path = directory / str(relative)
        if not path.resolve().is_relative_to(directory.resolve()):
            raise DomainError(400, "unsafe_path", "Invalid asset path")
        current = path
        while current != directory:
            if current.is_symlink():
                raise DomainError(400, "unsafe_path", "Symbolic link assets are not permitted")
            current = current.parent
        return path

    def get(self, video_id):
        self.directory(video_id)
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
        if row is None:
            raise DomainError(404, "video_not_found", "Video not found")
        return dict(row)

    def public(self, row):
        ready = bool(row["active_generation"])
        return {key: row[key] for key in ("video_id", "original_name", "size_bytes", "duration", "created_at", "status", "active_generation", "last_error")} | {
            "capabilities": {"playback": bool(row["playback_path"]), "vision": ready, "transcript": False},
            "content_url": f"/api/videos/{row['video_id']}/content" if row["playback_path"] else None,
        }

    def list(self, offset=0, limit=50):
        with self.db.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            rows = conn.execute("SELECT * FROM videos ORDER BY created_at DESC,video_id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return {"items": [self.public(dict(row)) for row in rows], "total": total, "offset": offset, "limit": limit}

    def register(self, video_id, original_name, source_path, size, duration):
        with self.db.connect(write=True) as conn:
            conn.execute("INSERT INTO videos(video_id,original_name,source_path,size_bytes,duration,created_at) VALUES(?,?,?,?,?,?)",
                         (video_id, original_name, source_path, size, duration, time.time()))
        return self.get(video_id)

    def update_video(self, video_id, **fields):
        allowed = {"status", "playback_path", "active_generation", "model_fingerprint", "last_error"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("invalid registry update")
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE videos SET " + ",".join(k + "=?" for k in fields) + " WHERE video_id=?", (*fields.values(), video_id))

    def create_job(self, video_id, max_queued):
        row = self.get(video_id)
        if row["status"] in {"deleting", "delete_failed"}:
            raise DomainError(409, "video_deleting", "Video deletion must finish before indexing")
        jid = uuid.uuid4().hex
        now = time.time()
        with self.db.connect(write=True) as conn:
            if conn.execute("SELECT 1 FROM index_jobs WHERE video_id=? AND status IN ('queued','running')", (video_id,)).fetchone():
                raise DomainError(409, "index_running", "This video already has an indexing job")
            if conn.execute("SELECT COUNT(*) FROM index_jobs WHERE status IN ('queued','running')").fetchone()[0] >= max_queued:
                raise DomainError(429, "queue_full", "Indexing queue is full; retry later")
            attempt = conn.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM index_jobs WHERE video_id=?", (video_id,)).fetchone()[0]
            conn.execute("INSERT INTO index_jobs(job_id,video_id,generation,status,phase,attempt,started_at,updated_at) VALUES(?,?,?,'queued','queued',?,?,?)",
                         (jid, video_id, jid, attempt, now, now))
            conn.execute("UPDATE videos SET status='queued',last_error=NULL WHERE video_id=?", (video_id,))
        return jid

    def job(self, job_id):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM index_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise DomainError(404, "job_not_found", "Job not found")
        result = dict(row)
        result["logs"] = json.loads(result["logs"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def update_job(self, job_id, **fields):
        allowed = {"status", "phase", "current", "total", "logs", "result", "error", "error_code"}
        if not fields.keys() <= allowed:
            raise ValueError("invalid job update")
        fields["updated_at"] = time.time()
        for key in ("logs", "result"):
            if key in fields:
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE index_jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE job_id=?", (*fields.values(), job_id))

    def publish(self, video_id, job_id, fingerprint, result, partial):
        status = "partial" if partial else "succeeded"
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE videos SET active_generation=?,model_fingerprint=?,status=?,last_error=NULL WHERE video_id=?",
                         (job_id, fingerprint, status, video_id))
            conn.execute("UPDATE index_jobs SET status=?,phase='done',result=?,updated_at=? WHERE job_id=?",
                         (status, json.dumps(result, ensure_ascii=False), time.time(), job_id))

    def recover(self):
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE videos SET status='interrupted',last_error='Server stopped before indexing finished' WHERE video_id IN (SELECT video_id FROM index_jobs WHERE status IN ('queued','running'))")
            conn.execute("UPDATE index_jobs SET status='interrupted',phase='interrupted',error_code='server_restart',error='Server stopped before indexing finished',updated_at=? WHERE status IN ('queued','running')", (time.time(),))
