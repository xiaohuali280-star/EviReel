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
        transcript = self.transcript_state(row)
        with self.db.connect() as conn:
            job = conn.execute("SELECT job_id FROM index_jobs WHERE video_id=? ORDER BY started_at DESC,attempt DESC LIMIT 1", (row['video_id'],)).fetchone()
        return {key: row[key] for key in ("video_id", "original_name", "size_bytes", "duration", "created_at", "status", "active_generation", "last_error", "trashed_at")} | {
            "latest_job_id": job[0] if job else None,
            "capabilities": {"playback": bool(row["playback_path"]), "vision": ready,
                             "transcript": transcript["readable"], "transcript_search": transcript["searchable"]},
            "transcript": {"status": row["transcript_status"], "error": row["transcript_error"], **transcript},
            "content_url": f"/api/videos/{row['video_id']}/content" if row["playback_path"] else None,
        }

    def transcript_state(self, row):
        from vam.memory.transcript import TranscriptStore
        generation = row["transcript_generation"]
        if generation:
            if not re.fullmatch(r"[0-9a-f]{32}", generation):
                raise DomainError(409, "invalid_index_version", "Invalid transcript memory version")
            path = self.asset(row["video_id"], f"memory/{generation}.sqlite3")
            if path.is_file():
                return TranscriptStore(path, row["video_id"]).state()
        return {"text_version": None, "search_version": None, "readable": False, "searchable": False,
                "text_status": "not_ready", "language": None, "search_uses_older_text": False}

    def list(self, offset=0, limit=50, *, q='', trash=False, video_ids=None):
        # Literal substring search: user '%'/'_' are not SQL wildcard operators.
        where = "trashed_at IS NOT NULL" if trash else "trashed_at IS NULL"
        where += " AND instr(lower(original_name),lower(?))>0"
        params = [q]
        if video_ids is not None:  # platform mode: restrict to the caller's workspace
            where += " AND video_id IN (SELECT value FROM json_each(?))"
            params.append(json.dumps(list(video_ids)))
        with self.db.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM videos WHERE " + where, params).fetchone()[0]
            rows = conn.execute("SELECT * FROM videos WHERE " + where + " ORDER BY created_at DESC,video_id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return {"items": [self.public(dict(row)) for row in rows], "total": total, "offset": offset, "limit": limit}

    def register(self, video_id, original_name, source_path, size, duration):
        with self.db.connect(write=True) as conn:
            conn.execute("INSERT INTO videos(video_id,original_name,source_path,size_bytes,duration,created_at) VALUES(?,?,?,?,?,?)",
                         (video_id, original_name, source_path, size, duration, time.time()))
        return self.get(video_id)

    def update_video(self, video_id, **fields):
        allowed = {"status", "playback_path", "active_generation", "model_fingerprint", "last_error",
                   "source_sha256", "transcript_generation", "transcript_status", "transcript_error", "trashed_at"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("invalid registry update")
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE videos SET " + ",".join(k + "=?" for k in fields) + " WHERE video_id=?", (*fields.values(), video_id))

    def create_job(self, video_id, max_queued, kind="video"):
        if kind not in {"video", "transcript"}:
            raise ValueError("unsupported indexing job kind")
        row = self.get(video_id)
        if row['trashed_at'] is not None:
            raise DomainError(409, 'video_trashed', 'Restore the video before indexing')
        if row["status"] in {"deleting", "delete_failed"}:
            raise DomainError(409, "video_deleting", "Video deletion must finish before indexing")
        jid = uuid.uuid4().hex
        now = time.time()
        with self.db.connect(write=True) as conn:
            if conn.execute("SELECT 1 FROM index_jobs WHERE video_id=? AND status IN ('queued','running')", (video_id,)).fetchone():
                raise DomainError(409, "index_running", "This video already has an indexing job")
            if conn.execute("SELECT COUNT(*) FROM index_jobs WHERE status IN ('queued','running')").fetchone()[0] >= max_queued:
                raise DomainError(429, "queue_full", "Indexing queue is full; retry later")
            attempt = conn.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM index_jobs WHERE video_id=? AND kind=?", (video_id, kind)).fetchone()[0]
            conn.execute("INSERT INTO index_jobs(job_id,video_id,generation,status,phase,attempt,started_at,updated_at,kind) VALUES(?,?,?,'queued','queued',?,?,?,?)",
                         (jid, video_id, jid, attempt, now, now, kind))
            conn.execute("UPDATE videos SET status='queued',last_error=NULL WHERE video_id=?", (video_id,))
        return jid

    def project_job(self, job_id, video_id, kind):
        """Worker mode: mirror a PostgreSQL job into index_jobs (the UI's read model). Idempotent.
        PostgreSQL allows one active job per video, so any other active row here is stale."""
        now = time.time()
        with self.db.connect(write=True) as conn:
            if conn.execute("SELECT 1 FROM index_jobs WHERE job_id=?", (job_id,)).fetchone():
                return
            conn.execute("UPDATE index_jobs SET status='interrupted',phase='interrupted',updated_at=? "
                         "WHERE video_id=? AND status IN ('queued','running')", (now, video_id))
            attempt = conn.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM index_jobs WHERE video_id=? AND kind=?",
                                   (video_id, kind)).fetchone()[0]
            conn.execute("INSERT INTO index_jobs(job_id,video_id,generation,status,phase,attempt,started_at,updated_at,kind) "
                         "VALUES(?,?,?,'queued','queued',?,?,?,?)", (job_id, video_id, job_id, attempt, now, now, kind))
            conn.execute("UPDATE videos SET status='queued',last_error=NULL WHERE video_id=?", (video_id,))

    def publish_fenced(self, video_id, job_id, *, fence, status, result, error_code=None, error=None,
                       vision_generation=None, fingerprint=None, playback_path=None, transcript_status=None,
                       transcript_error=None) -> bool:
        """Worker-mode publication: every user-visible field of the video plus the job's read-model row,
        in one transaction, only if no newer job (higher fence) has published. Re-running the same job's
        publication writes identical values (crash-recovery between this commit and the ledger commit)."""
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT published_seq FROM videos WHERE video_id=?", (video_id,)).fetchone()
            if row is None or row[0] > fence:
                return False
            fields = {"published_seq": fence, "status": status, "last_error": error}
            if vision_generation:
                fields |= {"active_generation": vision_generation, "model_fingerprint": fingerprint}
            if playback_path:
                fields["playback_path"] = playback_path
            if transcript_status:
                fields |= {"transcript_status": transcript_status, "transcript_error": transcript_error}
            conn.execute("UPDATE videos SET " + ",".join(k + "=?" for k in fields) + " WHERE video_id=?",
                         (*fields.values(), video_id))
            conn.execute("UPDATE index_jobs SET status=?,phase=?,result=?,error_code=?,error=?,updated_at=? WHERE job_id=?",
                         (status, 'done' if status in {'succeeded', 'partial'} else status,
                          json.dumps(result, ensure_ascii=False), error_code, error, time.time(), job_id))
            return True

    # -- worker-mode notes jobs: study_jobs/study_artifacts are the read model of PostgreSQL notes jobs
    def project_study_job(self, job_id, video_id, request_id):
        now = time.time()
        with self.db.connect(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO study_jobs(job_id,video_id,request_id,options,status,phase,created_at,updated_at) "
                         "VALUES(?,?,?,'{}','queued','queued',?,?)", (job_id, video_id, request_id, now, now))

    def progress_study_job(self, job_id, **fields):
        allowed = {"status", "phase", "current", "total", "error_code"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("invalid study job update")
        fields["updated_at"] = time.time()
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE study_jobs SET " + ",".join(k + "=?" for k in fields) +
                         " WHERE job_id=? AND status IN ('queued','running')", (*fields.values(), job_id))

    def publish_study_artifact(self, video_id, job_id, *, artifact_id, versions, content, created_at):
        """Idempotent (deterministic id); ordered by the job's creation time, so a late publication of
        an older notes job can never become the "latest" artifact."""
        with self.db.connect(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO study_artifacts VALUES(?,?,?,?,?,?)",
                         (artifact_id, video_id, job_id, json.dumps(versions), json.dumps(content, ensure_ascii=False),
                          created_at))
            conn.execute("UPDATE study_jobs SET status='succeeded',phase='done',artifact_id=?,updated_at=? WHERE job_id=?",
                         (artifact_id, time.time(), job_id))

    def publish_study_artifact_state(self, job_id, artifact_id):
        """Ledger-derived terminal state of a successful notes job (idempotent)."""
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE study_jobs SET status='succeeded',phase='done',artifact_id=?,error_code=NULL,updated_at=? "
                         "WHERE job_id=?", (artifact_id, time.time(), job_id))

    def finish_study_job(self, job_id, *, status, error_code=None):
        """Ledger-derived terminal state of a notes job that did not publish (idempotent)."""
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE study_jobs SET status=?,phase=?,error_code=?,updated_at=? WHERE job_id=?",
                         (status, status, error_code, time.time(), job_id))

    def progress_job(self, job_id, **fields):
        """Worker progress: never reopens a job row that already reached a final state."""
        allowed = {"status", "phase", "current", "total", "error", "error_code"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("invalid job update")
        fields["updated_at"] = time.time()
        with self.db.connect(write=True) as conn:
            conn.execute("UPDATE index_jobs SET " + ",".join(k + "=?" for k in fields) +
                         " WHERE job_id=? AND status IN ('queued','running')", (*fields.values(), job_id))

    def finish(self, video_id, job_id, *, status, result, error_code=None, error=None, vision_generation=None, fingerprint=None):
        with self.db.connect(write=True) as conn:
            if vision_generation:
                conn.execute("UPDATE videos SET active_generation=?,model_fingerprint=? WHERE video_id=?", (vision_generation, fingerprint, video_id))
            conn.execute("UPDATE videos SET status=?,last_error=? WHERE video_id=?", (status, error, video_id))
            conn.execute("UPDATE index_jobs SET status=?,phase=?,result=?,error_code=?,error=?,updated_at=? WHERE job_id=?",
                         (status, 'done' if status in {'succeeded', 'partial'} else status,
                          json.dumps(result, ensure_ascii=False), error_code, error, time.time(), job_id))

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
            conn.execute("UPDATE videos SET transcript_status='interrupted',transcript_error='Server stopped during transcript indexing' WHERE transcript_status='running' AND video_id IN (SELECT video_id FROM index_jobs WHERE status IN ('queued','running'))")
            conn.execute("UPDATE videos SET status='interrupted',last_error='Server stopped before indexing finished' WHERE video_id IN (SELECT video_id FROM index_jobs WHERE status IN ('queued','running'))")
            conn.execute("UPDATE index_jobs SET status='interrupted',phase='interrupted',error_code='server_restart',error='Server stopped before indexing finished',updated_at=? WHERE status IN ('queued','running')", (time.time(),))
