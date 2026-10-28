"""Resumable direct-to-storage uploads (E1).

State machine (PostgreSQL ``upload_sessions.state``)::

    initiated -> uploading -> completing -> verifying -> completed
         |           |            |             |
         v           v            v             v
       failed   aborted/expired  uploading    failed
                                (parts missing)

Correctness rules
* Every transition is a conditional UPDATE on the current state (and, for
  verification, the ``state_version`` attempt token), so cancel/expiry/complete
  races are decided by the database, never by timing.
* Quota is reserved atomically at creation under a workspace row lock and is
  settled or released in the same transaction as the terminal transition, so it
  changes exactly once per session.
* The storage multipart listing (ListParts) is the source of truth for upload
  progress; ``upload_parts`` is the ledger it is reconciled into.
* Object assembly (completing) and media verification (verifying) are separate
  steps; only a verified object is registered as a usable video.
* Merge-succeeded/DB-failed windows are closed by ``reconcile()``: a session
  stuck in completing/verifying past its lease is re-driven idempotently.

Single-instance assumption (E1): background verification runs as in-process
asyncio tasks of the API process that owns DATA_ROOT. Leases plus the attempt
token keep a stale verifier from publishing, but E1 does not claim multi-API
execution; E2 moves this onto durable workers.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import math
import shutil
import uuid
from pathlib import Path

from vam.indexing.media import ALLOWED_EXTENSIONS, inspect_upload
from vam.platform.object_store import InvalidPart, NoSuchObject, NoSuchUpload, ObjectStoreError, StoredPart
from vam.runtime import run_sync
from vam.video_registry import DomainError

logger = logging.getLogger(__name__)

MiB = 1 << 20
MIN_PART = 5 * MiB          # S3 minimum for all but the last part
MAX_PARTS = 10000
ACTIVE_STATES = ("initiated", "uploading", "completing", "verifying")
CONTENT_TYPES = {".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime",
                 ".mkv": "video/x-matroska", ".webm": "video/webm", ".avi": "video/x-msvideo"}


def plan_parts(size: int, preferred: int) -> tuple[int, int]:
    """Return (part_size, part_count) honouring the S3 5 MiB / 10,000-part limits."""
    if size <= 0:
        raise ValueError("size must be positive")
    part = max(int(preferred), MIN_PART, math.ceil(size / MAX_PARTS))
    part = math.ceil(part / MiB) * MiB
    return part, max(1, math.ceil(size / part))


def part_length(size: int, part_size: int, part_count: int, number: int) -> int:
    if not 1 <= number <= part_count:
        raise ValueError("part number out of range")
    return part_size if number < part_count else size - part_size * (part_count - 1)


def md5_hex(md5_b64: str) -> str:
    return base64.b64decode(md5_b64).hex()


def composite_etag(md5s_b64: list[str]) -> str:
    """S3 multipart ETag: md5(concat(part md5 digests)) + '-N'. Not a whole-file MD5."""
    digest = hashlib.md5(b"".join(base64.b64decode(m) for m in md5s_b64)).hexdigest()
    return f"{digest}-{len(md5s_b64)}"


def _valid_md5_b64(value: str) -> bool:
    try:
        return len(value) == 24 and len(base64.b64decode(value, validate=True)) == 16
    except (binascii.Error, ValueError):
        return False


def _safe_name(name: str) -> str:
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return "".join(ch for ch in name if ch.isprintable())[:255]


class UploadService:
    def __init__(self, db, store, identity, indexing, settings):
        self.db, self.store, self.identity, self.indexing, self.settings = db, store, identity, indexing, settings
        self.registry = indexing.registry
        self.tasks: dict[str, asyncio.Task] = {}
        self.closed = False
        self.reconciler: asyncio.Task | None = None
        self.prefix = settings.s3_key_prefix.strip("/") + "/" if settings.s3_key_prefix.strip("/") else ""

    # ------------------------------------------------------------------ lifecycle
    async def start(self):
        for stale in (self.registry.root / ".uploads").glob("*.verify"):
            stale.unlink(missing_ok=True)  # interrupted verification downloads; the object is still in storage
        await run_sync(self.store.ensure_bucket)
        await self.reconcile()
        self.reconciler = asyncio.create_task(self._reconcile_loop())

    async def close(self):
        self.closed = True
        tasks = [t for t in [self.reconciler, *self.tasks.values()] if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _reconcile_loop(self):
        while not self.closed:
            await asyncio.sleep(self.settings.upload_reconcile_interval_s)
            try:
                await self.reconcile()
            except Exception:
                logger.exception("upload_reconcile_failed")

    # ------------------------------------------------------------------ helpers
    def _row(self, conn, upload_id, *, lock=False):
        if not (isinstance(upload_id, str) and upload_id.startswith("up_") and len(upload_id) == 35):
            raise DomainError(404, "upload_not_found", "Upload session not found")
        row = conn.execute("SELECT * FROM upload_sessions WHERE upload_id=%s" + (" FOR UPDATE" if lock else ""),
                           (upload_id,)).fetchone()
        if row is None:
            raise DomainError(404, "upload_not_found", "Upload session not found")
        return row

    def _authorized(self, principal, row):
        # Foreign sessions are indistinguishable from missing ones.
        if row["workspace_id"] not in principal.workspaces:
            raise DomainError(404, "upload_not_found", "Upload session not found")
        return row

    def _parts(self, conn, upload_id):
        return {r["part_number"]: r for r in conn.execute(
            "SELECT * FROM upload_parts WHERE upload_id=%s ORDER BY part_number", (upload_id,))}

    def view(self, row, parts=None):
        parts = parts or {}
        uploaded = sorted(n for n, p in parts.items() if p["state"] == "uploaded")
        mismatched = sorted(n for n, p in parts.items() if p["state"] == "mismatch")
        size = lambda n: part_length(row["declared_size"], row["part_size"], row["part_count"], n)
        return {
            "upload_id": row["upload_id"], "video_id": row["video_id"], "state": row["state"],
            "original_name": row["original_name"], "declared_size": row["declared_size"],
            "part_size": row["part_size"], "part_count": row["part_count"],
            "client_fingerprint": row["client_fingerprint"],
            "uploaded_parts": uploaded, "mismatched_parts": mismatched,
            "uploaded_bytes": sum(size(n) for n in uploaded),
            "missing_parts": [n for n in range(1, row["part_count"] + 1) if n not in set(uploaded)]
            if row["state"] in ("uploading", "initiated") else [],
            "expires_at": row["expires_at"].isoformat(), "created_at": row["created_at"].isoformat(),
            "error": {"code": row["error_code"], "message": row["error_message"]} if row["error_code"] else None,
            "result": row["result"], "job_id": row["job_id"],
        }

    def _release(self, conn, row, state, code=None, message=None, *, expect_states=None, version=None):
        """Terminal non-success transition + quota release in the caller's transaction."""
        sql = ("UPDATE upload_sessions SET state=%s, quota_state=CASE WHEN quota_state='reserved' THEN 'released' "
               "ELSE quota_state END, error_code=COALESCE(%s, error_code), error_message=COALESCE(%s, error_message), "
               "state_version=state_version+1, lease_until=NULL, updated_at=now() WHERE upload_id=%s AND state = ANY(%s)")
        params = [state, code, message, row["upload_id"], list(expect_states or [row["state"]])]
        if version is not None:
            sql += " AND state_version=%s"
            params.append(version)
        updated = conn.execute(sql + " RETURNING upload_id", params).fetchone()
        if updated and row["quota_state"] == "reserved":
            conn.execute("UPDATE workspaces SET reserved_bytes=reserved_bytes-%s WHERE workspace_id=%s",
                         (row["declared_size"], row["workspace_id"]))
        return bool(updated)

    # ------------------------------------------------------------------ create
    def create(self, principal, *, name, size, fingerprint, content_type=""):
        name = _safe_name(name)
        ext = Path(name).suffix.lower()
        if not name or ext not in ALLOWED_EXTENSIONS:
            raise DomainError(415, "unsupported_media", "Supported video extensions: " + ", ".join(sorted(ALLOWED_EXTENSIONS)))
        if content_type and not (content_type.startswith("video/") or content_type == "application/octet-stream"):
            raise DomainError(415, "unsupported_media", "Expected a video upload")
        if not isinstance(size, int) or size <= 0:
            raise DomainError(422, "empty_upload", "Video upload is empty")
        if size > self.settings.max_upload_bytes:
            raise DomainError(413, "upload_limit", "Upload exceeds configured size limit")
        if not (isinstance(fingerprint, str) and 8 <= len(fingerprint) <= 128):
            raise DomainError(422, "invalid_fingerprint", "File fingerprint is required")
        part_size, part_count = plan_parts(size, self.settings.upload_part_size)
        upload_id, video_id = "up_" + uuid.uuid4().hex, "v_" + uuid.uuid4().hex
        key = f"{self.prefix}workspaces/{principal.workspace_id}/uploads/{upload_id}/source{ext}"
        with self.db.transaction() as conn:
            ws = conn.execute("SELECT * FROM workspaces WHERE workspace_id=%s FOR UPDATE",
                              (principal.workspace_id,)).fetchone()
            active = conn.execute("SELECT COUNT(*) AS n FROM upload_sessions WHERE workspace_id=%s AND state = ANY(%s)",
                                  (principal.workspace_id, list(ACTIVE_STATES))).fetchone()["n"]
            if active >= self.settings.upload_max_active_sessions:
                raise DomainError(429, "too_many_uploads", "Too many unfinished uploads; finish or cancel one first")
            if ws["used_bytes"] + ws["reserved_bytes"] + size > ws["quota_bytes"]:
                raise DomainError(413, "quota_exceeded", "Workspace storage quota exceeded")
            conn.execute("UPDATE workspaces SET reserved_bytes=reserved_bytes+%s WHERE workspace_id=%s",
                         (size, principal.workspace_id))
            conn.execute(
                "INSERT INTO upload_sessions(upload_id, workspace_id, created_by, video_id, original_name, extension, "
                "content_type, object_key, declared_size, part_size, part_count, client_fingerprint, state, quota_state, "
                "expires_at, lease_until) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'initiated','reserved', "
                "now() + make_interval(secs => %s), now() + make_interval(secs => %s))",
                (upload_id, principal.workspace_id, principal.user_id, video_id, name, ext, CONTENT_TYPES[ext], key,
                 size, part_size, part_count, fingerprint, self.settings.upload_session_ttl_s, self.settings.upload_lease_s))
        try:
            storage_id = self.store.create_multipart(key, CONTENT_TYPES[ext])
        except ObjectStoreError as exc:
            with self.db.transaction() as conn:
                self._release(conn, self._row(conn, upload_id, lock=True), "failed", "storage_unavailable",
                              "Object storage is unavailable; retry the upload")
            raise DomainError(503, "storage_unavailable", "Object storage is unavailable; retry later") from exc
        with self.db.transaction() as conn:
            row = conn.execute("UPDATE upload_sessions SET storage_upload_id=%s, state='uploading', lease_until=NULL, "
                               "state_version=state_version+1, updated_at=now() WHERE upload_id=%s AND state='initiated' "
                               "RETURNING *", (storage_id, upload_id)).fetchone()
        if row is None:  # reconciler already failed the initiation; do not leak the multipart upload
            self.store.abort_multipart(key, storage_id)
            raise DomainError(503, "upload_init_interrupted", "Upload initialization was interrupted; retry")
        return self.view(row)

    # ------------------------------------------------------------------ progress
    def _sync_parts(self, row):
        """Reconcile the ledger with what storage actually holds (ListParts is authoritative)."""
        try:
            stored = {p.part_number: p for p in self.store.list_parts(row["object_key"], row["storage_upload_id"])}
        except NoSuchUpload:
            return None
        with self.db.transaction() as conn:
            ledger = self._parts(conn, row["upload_id"])
            for number, ledger_row in ledger.items():
                part = stored.get(number)
                if part is None:
                    state, etag = "signed", None
                elif (part.size != part_length(row["declared_size"], row["part_size"], row["part_count"], number)
                      or (self.settings.upload_verify_part_etag and part.etag != md5_hex(ledger_row["md5_b64"]))):
                    state, etag = "mismatch", part.etag
                else:
                    state, etag = "uploaded", part.etag
                if (state, etag) != (ledger_row["state"], ledger_row["etag"]):
                    conn.execute("UPDATE upload_parts SET state=%s, etag=%s, uploaded_at=CASE WHEN %s='uploaded' "
                                 "THEN COALESCE(uploaded_at, now()) END WHERE upload_id=%s AND part_number=%s",
                                 (state, etag, state, row["upload_id"], number))
            # Parts present in storage but never signed by us cannot be trusted.
            for number in set(stored) - set(ledger):
                logger.warning("unsigned_part upload_id=%s part=%s", row["upload_id"], number)
            return self._parts(conn, row["upload_id"])

    def get(self, principal, upload_id):
        with self.db.connect() as conn:
            row = self._authorized(principal, self._row(conn, upload_id))
            parts = self._parts(conn, upload_id)
        if row["state"] == "uploading":
            parts = self._sync_parts(row) or parts
        return self.view(row, parts)

    def list_active(self, principal):
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM upload_sessions WHERE workspace_id=%s AND state = ANY(%s) "
                                "ORDER BY created_at DESC LIMIT 50",
                                (principal.workspace_id, list(ACTIVE_STATES))).fetchall()
            return [self.view(r, self._parts(conn, r["upload_id"])) for r in rows]

    def sign(self, principal, upload_id, parts):
        if not parts or len(parts) > 100:
            raise DomainError(422, "invalid_parts", "Sign between 1 and 100 parts per request")
        with self.db.transaction() as conn:
            row = self._authorized(principal, self._row(conn, upload_id, lock=True))
            closed = self._check_open(conn, row)
            if closed is None:
                for part in parts:
                    number, md5, size = part.get("part_number"), part.get("md5_b64"), part.get("size")
                    if not isinstance(number, int) or not 1 <= number <= row["part_count"]:
                        raise DomainError(422, "invalid_part_number", "Part number out of range")
                    if size != part_length(row["declared_size"], row["part_size"], row["part_count"], number):
                        raise DomainError(422, "invalid_part_size", f"Part {number} must be exactly the planned size")
                    if not isinstance(md5, str) or not _valid_md5_b64(md5):
                        raise DomainError(422, "invalid_part_checksum", "Content-MD5 (base64) is required for every part")
                    conn.execute(
                        "INSERT INTO upload_parts(upload_id, part_number, size, md5_b64, state) VALUES (%s,%s,%s,%s,'signed') "
                        "ON CONFLICT (upload_id, part_number) DO UPDATE SET size=EXCLUDED.size, md5_b64=EXCLUDED.md5_b64, "
                        "state=CASE WHEN upload_parts.state='uploaded' AND upload_parts.md5_b64=EXCLUDED.md5_b64 "
                        "THEN 'uploaded' ELSE 'signed' END, signed_at=now()", (upload_id, number, size, md5))
        if closed is not None:
            self._after_close(row, closed)
        ttl = self.settings.upload_url_ttl_s
        urls = [{"part_number": p["part_number"], "size": p["size"], "url": self.store.presign_upload_part(
            row["object_key"], row["storage_upload_id"], p["part_number"], p["md5_b64"], p["size"], ttl)} for p in parts]
        return {"upload_id": upload_id, "expires_in": ttl, "parts": urls}

    def _check_open(self, conn, row):
        """Return the error to raise *after* commit (an expiry transition must persist), or None."""
        if row["state"] == "uploading" and self._expired(conn, row):
            self._release(conn, row, "expired", "upload_expired", "Upload session expired")
            return DomainError(410, "upload_expired", "Upload session expired; start a new upload")
        if row["state"] != "uploading":
            return DomainError(409, f"upload_{row['state']}", f"Upload session is {row['state']}")
        return None

    def _after_close(self, row, error):
        """Runs after the expiry transition committed: free the storage upload, then report."""
        if error.code == "upload_expired" and row["storage_upload_id"]:
            try:
                self.store.abort_multipart(row["object_key"], row["storage_upload_id"])
            except ObjectStoreError:
                pass  # orphan sweep retries
        raise error

    @staticmethod
    def _expired(conn, row):
        return conn.execute("SELECT %s <= now() AS e", (row["expires_at"],)).fetchone()["e"]

    # ------------------------------------------------------------------ cancel
    def cancel(self, principal, upload_id):
        with self.db.transaction() as conn:
            row = self._authorized(principal, self._row(conn, upload_id, lock=True))
            if row["state"] in ("aborted", "expired", "failed"):
                return self.view(row)
            if row["state"] in ("completing", "verifying", "completed"):
                raise DomainError(409, "upload_not_cancellable", f"Upload is already {row['state']}")
            self._release(conn, row, "aborted", "upload_cancelled", "Upload cancelled by user")
        if row["storage_upload_id"]:
            try:
                self.store.abort_multipart(row["object_key"], row["storage_upload_id"])
            except ObjectStoreError:
                logger.warning("abort_deferred upload_id=%s", upload_id)  # orphan sweep retries
        return self.current_view(upload_id)

    # ------------------------------------------------------------------ complete
    def begin_complete(self, principal, upload_id):
        """Claim the merge. Returns (claimed, view). Repeats return the existing outcome."""
        with self.db.transaction() as conn:
            row = self._authorized(principal, self._row(conn, upload_id, lock=True))
            if row["state"] in ("completing", "verifying", "completed"):
                return False, self.view(row)
            closed = self._check_open(conn, row)
            if closed is None:
                row = conn.execute("UPDATE upload_sessions SET state='completing', state_version=state_version+1, "
                                   "lease_until=now() + make_interval(secs => %s), updated_at=now() "
                                   "WHERE upload_id=%s AND state='uploading' RETURNING *",
                                   (self.settings.upload_lease_s, upload_id)).fetchone()
        if closed is not None:
            self._after_close(row, closed)
        return True, self.view(row)

    def merge(self, upload_id):
        """Assemble the object (idempotent). Leaves the session in verifying, uploading or failed."""
        with self.db.connect() as conn:
            row = self._row(conn, upload_id)
        if row["state"] != "completing":
            return row["state"]
        try:
            stored = self.store.list_parts(row["object_key"], row["storage_upload_id"])
        except NoSuchUpload:
            stored = None  # already assembled by an earlier attempt (or lost) -> check the object
        if stored is not None:
            ledger = self._sync_parts(row) or {}
            ok = {n for n, p in ledger.items() if p["state"] == "uploaded"}
            missing = [n for n in range(1, row["part_count"] + 1) if n not in ok]
            if missing:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE upload_sessions SET state='uploading', lease_until=NULL, "
                                 "state_version=state_version+1, updated_at=now() WHERE upload_id=%s AND state='completing'",
                                 (upload_id,))
                raise DomainError(409, "parts_missing", f"{len(missing)} part(s) missing or invalid: "
                                  + ",".join(map(str, missing[:50])))
            parts = [StoredPart(n, p["size"], p["etag"]) for n, p in sorted(ledger.items()) if n in ok]
            try:
                self.store.complete_multipart(row["object_key"], row["storage_upload_id"], parts)
            except NoSuchUpload:
                pass
            except InvalidPart as exc:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE upload_sessions SET state='uploading', lease_until=NULL, "
                                 "state_version=state_version+1, updated_at=now() WHERE upload_id=%s AND state='completing'",
                                 (upload_id,))
                raise DomainError(409, "parts_invalid", "Storage rejected the part list; re-sync and retry") from exc
        try:
            info = self.store.head(row["object_key"])
        except NoSuchObject:
            with self.db.transaction() as conn:
                self._release(conn, self._row(conn, upload_id, lock=True), "failed", "storage_upload_lost",
                              "Uploaded parts were not found in storage; upload again", expect_states=["completing"])
            return "failed"
        if info.size != row["declared_size"]:
            with self.db.transaction() as conn:
                self._release(conn, self._row(conn, upload_id, lock=True), "failed", "size_mismatch",
                              "Assembled file size does not match the declared size", expect_states=["completing"])
            self._delete_object(row)
            return "failed"
        self._mark_verifying(upload_id)
        return "verifying"

    def _mark_verifying(self, upload_id):
        # Seam for fault-injection tests: merged object exists, DB transition may fail.
        with self.db.transaction() as conn:
            conn.execute("UPDATE upload_sessions SET state='verifying', state_version=state_version+1, lease_until=NULL, "
                         "updated_at=now() WHERE upload_id=%s AND state='completing'", (upload_id,))

    async def complete(self, principal, upload_id):
        claimed, view = await run_sync(self.begin_complete, principal, upload_id)
        if claimed:
            state = await run_sync(self.merge, upload_id)
            if state == "verifying":
                self.start_verification(upload_id)
            view = await run_sync(self.current_view, upload_id)
        return view

    def current_view(self, upload_id):
        with self.db.connect() as conn:
            return self.view(self._row(conn, upload_id), self._parts(conn, upload_id))

    # ------------------------------------------------------------------ verify
    def start_verification(self, upload_id):
        if self.closed or (upload_id in self.tasks and not self.tasks[upload_id].done()):
            return
        task = asyncio.create_task(self._verify(upload_id))
        self.tasks[upload_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(upload_id, None) if self.tasks.get(upload_id) is task else None)

    def _claim_verification(self, upload_id):
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE upload_sessions SET state_version=state_version+1, verify_attempts=verify_attempts+1, "
                "lease_until=now() + make_interval(secs => %s), updated_at=now() WHERE upload_id=%s AND state='verifying' "
                "AND (lease_until IS NULL OR lease_until < now()) RETURNING *",
                (self.settings.upload_lease_s, upload_id)).fetchone()

    def _heartbeat(self, upload_id, version):
        with self.db.transaction() as conn:
            if not conn.execute("UPDATE upload_sessions SET lease_until=now() + make_interval(secs => %s) "
                                "WHERE upload_id=%s AND state='verifying' AND state_version=%s RETURNING 1",
                                (self.settings.upload_lease_s, upload_id, version)).fetchone():
                raise _LeaseLost()

    def _download_and_check(self, row):
        """Stream the assembled object once: trusted size + SHA-256, then probe the media."""
        version = row["state_version"]
        staging = self.registry.root / ".uploads" / f"{row['upload_id']}.{version}.verify"
        digest, size, since_beat = hashlib.sha256(), 0, 0
        try:
            with staging.open("wb") as handle:
                for chunk in self.store.iter_object(row["object_key"]):
                    size += len(chunk)
                    if size > row["declared_size"]:
                        raise DomainError(422, "size_mismatch", "Stored object is larger than declared")
                    digest.update(chunk)
                    handle.write(chunk)
                    since_beat += len(chunk)
                    if since_beat >= 64 * MiB:
                        self._heartbeat(row["upload_id"], version)
                        since_beat = 0
            if size != row["declared_size"]:
                raise DomainError(422, "size_mismatch", "Stored object size does not match the declared size")
            with self.db.connect() as conn:
                parts = self._parts(conn, row["upload_id"])
            if self.settings.upload_verify_part_etag and row["part_count"] > 1 and len(parts) == row["part_count"]:
                expected = composite_etag([parts[n]["md5_b64"] for n in range(1, row["part_count"] + 1)])
                actual = self.store.head(row["object_key"]).etag
                if "-" in actual and actual != expected:
                    raise DomainError(422, "integrity_mismatch", "Assembled object does not match the signed parts")
            meta = inspect_upload(staging, self.settings)
            return staging, size, digest.hexdigest(), meta
        except BaseException:
            staging.unlink(missing_ok=True)
            raise

    def _publish(self, row, staging, size, sha256, meta):
        """Make the verified file a usable video, then commit ownership + quota in one transaction."""
        video_id, ext = row["video_id"], row["extension"]
        directory = self.registry.directory(video_id)
        directory.mkdir(exist_ok=True)
        staging.replace(directory / ("source" + ext))
        try:
            self.registry.get(video_id)
        except DomainError:
            self.registry.register(video_id, row["original_name"], "source" + ext, size, meta["duration"])
        self.registry.update_video(video_id, source_sha256=sha256)
        result = {"video_id": video_id, "size": size, "sha256": sha256, "duration": meta["duration"]}
        with self.db.transaction() as conn:
            done = conn.execute(
                "UPDATE upload_sessions SET state='completed', quota_state='settled', verified_size=%s, verified_sha256=%s, "
                "result=%s, error_code=NULL, error_message=NULL, lease_until=NULL, completed_at=now(), updated_at=now(), "
                "state_version=state_version+1 WHERE upload_id=%s AND state='verifying' AND state_version=%s RETURNING *",
                (size, sha256, json.dumps(result), row["upload_id"], row["state_version"])).fetchone()
            if not done:
                raise _LeaseLost()
            conn.execute("UPDATE workspaces SET reserved_bytes=reserved_bytes-%s, used_bytes=used_bytes+%s "
                         "WHERE workspace_id=%s", (row["declared_size"], size, row["workspace_id"]))
            conn.execute("INSERT INTO video_ownership(video_id, workspace_id, origin, upload_id, object_key, size_bytes, sha256) "
                         "VALUES (%s,%s,'upload',%s,%s,%s,%s)",
                         (video_id, row["workspace_id"], row["upload_id"], row["object_key"], size, sha256))

    def _fail_verification(self, row, exc: DomainError):
        with self.db.transaction() as conn:
            failed = self._release(conn, row, "failed", exc.code, exc.message, expect_states=["verifying"],
                                   version=row["state_version"])
        if failed:
            self._delete_object(row)
            try:
                self.registry.get(row["video_id"])
            except DomainError:
                directory = self.registry.directory(row["video_id"])
                if directory.exists():
                    shutil.rmtree(directory)

    def _transient_failure(self, row, exc):
        """Leave the session in verifying; the lease expiry lets reconcile() retry, bounded by attempts."""
        if row["verify_attempts"] >= self.settings.upload_verify_attempts:
            self._fail_verification(row, DomainError(503, "verification_unavailable",
                                                     "Verification could not run; upload again later"))
        else:
            logger.warning("verify_retry upload_id=%s error=%s", row["upload_id"], type(exc).__name__)

    async def _verify(self, upload_id):
        row = await run_sync(self._claim_verification, upload_id)
        if row is None:
            return
        try:
            staging, size, sha256, meta = await run_sync(self._download_and_check, row)
            await run_sync(self._publish, row, staging, size, sha256, meta)
        except _LeaseLost:
            logger.warning("verify_lease_lost upload_id=%s", upload_id)
            return
        except DomainError as exc:
            if exc.status >= 500:  # e.g. ffprobe missing -> operator problem, retry later
                await run_sync(self._transient_failure, row, exc)
            else:
                await run_sync(self._fail_verification, row, exc)
            return
        except (ObjectStoreError, OSError) as exc:
            await run_sync(self._transient_failure, row, exc)
            return
        await self.accept_processing(upload_id)

    async def accept_processing(self, upload_id):
        """Hand a completed upload to the existing indexing pipeline exactly once per session."""
        with self.db.connect() as conn:
            row = self._row(conn, upload_id)
        if row["state"] != "completed" or row["job_id"]:
            return row["job_id"]
        video_id = row["video_id"]
        # No await between the check and schedule(): atomic within this event loop.
        with self.registry.db.connect() as conn:
            existing = conn.execute("SELECT job_id FROM index_jobs WHERE video_id=? ORDER BY started_at LIMIT 1",
                                    (video_id,)).fetchone()
        index_error = None
        if existing:
            job_id = existing[0]
        else:
            try:
                job_id = self.indexing.schedule(video_id)
            except DomainError as exc:
                job_id, index_error = None, {"code": exc.code, "message": exc.message}
        def record():
            with self.db.transaction() as conn:
                if job_id:
                    conn.execute("UPDATE upload_sessions SET job_id=%s, updated_at=now() WHERE upload_id=%s AND job_id IS NULL",
                                 (job_id, upload_id))
                else:
                    conn.execute("UPDATE upload_sessions SET result = result || %s::jsonb, updated_at=now() "
                                 "WHERE upload_id=%s", (json.dumps({"index_error": index_error}), upload_id))
        await run_sync(record)
        return job_id

    def _delete_object(self, row):
        try:
            self.store.delete(row["object_key"])
        except ObjectStoreError:
            logger.warning("object_delete_deferred upload_id=%s", row["upload_id"])

    # ------------------------------------------------------------------ reconcile
    async def reconcile(self):
        """Idempotent recovery pass; safe to run at any time and repeatedly."""
        summary = {"expired": 0, "initiation_failed": 0, "merged": 0, "verify_restarted": 0,
                   "processing_accepted": 0, "orphan_multipart_aborted": 0, "orphan_ownership_released": 0}
        def due():
            with self.db.connect() as conn:
                return conn.execute(
                    "SELECT * FROM upload_sessions WHERE (state='uploading' AND expires_at <= now()) "
                    "OR (state IN ('initiated','completing','verifying') AND (lease_until IS NULL OR lease_until < now())) "
                    "OR (state='completed' AND job_id IS NULL AND NOT (result ? 'index_error' AND updated_at > now() - interval '60 seconds')) "
                    "ORDER BY updated_at LIMIT 200").fetchall()
        for row in await run_sync(due):
            try:
                if row["state"] == "uploading":
                    if await run_sync(self._expire, row):
                        summary["expired"] += 1
                elif row["state"] == "initiated":
                    if await run_sync(self._fail_initiation, row):
                        summary["initiation_failed"] += 1
                elif row["state"] == "completing":
                    claimed = await run_sync(self._reclaim_completing, row)
                    if claimed and await run_sync(self.merge, row["upload_id"]) == "verifying":
                        summary["merged"] += 1
                        self.start_verification(row["upload_id"])
                elif row["state"] == "verifying":
                    summary["verify_restarted"] += 1
                    self.start_verification(row["upload_id"])
                elif row["state"] == "completed":
                    if await self.accept_processing(row["upload_id"]):
                        summary["processing_accepted"] += 1
            except DomainError:
                pass  # e.g. parts_missing -> session returned to uploading for the client to finish
            except Exception:
                logger.exception("reconcile_session_failed upload_id=%s", row["upload_id"])
        summary["orphan_multipart_aborted"] = await run_sync(self._abort_orphan_multipart)
        summary["orphan_ownership_released"] = await run_sync(self._release_orphan_ownership)
        return summary

    def _expire(self, row):
        with self.db.transaction() as conn:
            current = self._row(conn, row["upload_id"], lock=True)
            if current["state"] != "uploading" or not self._expired(conn, current):
                return False
            self._release(conn, current, "expired", "upload_expired", "Upload session expired")
        try:
            self.store.abort_multipart(row["object_key"], row["storage_upload_id"])
        except ObjectStoreError:
            pass
        return True

    def _fail_initiation(self, row):
        with self.db.transaction() as conn:
            current = self._row(conn, row["upload_id"], lock=True)
            if current["state"] != "initiated" or not self._expired(conn, {"expires_at": current["lease_until"]}):
                return False
            self._release(conn, current, "failed", "upload_init_interrupted", "Upload initialization was interrupted")
        for key, storage_id, _ in self.store.list_multipart_uploads(row["object_key"]):
            if key == row["object_key"]:
                self.store.abort_multipart(key, storage_id)
        return True

    def _reclaim_completing(self, row):
        with self.db.transaction() as conn:
            return bool(conn.execute(
                "UPDATE upload_sessions SET lease_until=now() + make_interval(secs => %s), state_version=state_version+1 "
                "WHERE upload_id=%s AND state='completing' AND lease_until < now() RETURNING 1",
                (self.settings.upload_lease_s, row["upload_id"])).fetchone())

    def _abort_orphan_multipart(self):
        """Abort storage multipart uploads with no live session (e.g. after a crash), after a grace period."""
        import time
        uploads = self.store.list_multipart_uploads(self.prefix + "workspaces/")
        if not uploads:
            return 0
        with self.db.connect() as conn:
            live = {r["storage_upload_id"] for r in conn.execute(
                "SELECT storage_upload_id FROM upload_sessions WHERE state = ANY(%s) AND storage_upload_id IS NOT NULL",
                (list(ACTIVE_STATES),))}
        aborted = 0
        for key, storage_id, initiated in uploads:
            if storage_id not in live and time.time() - initiated > self.settings.upload_lease_s:
                self.store.abort_multipart(key, storage_id)
                aborted += 1
        return aborted

    def _release_orphan_ownership(self):
        """Ownership whose SQLite video was deleted (crash between the two stores) is released."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT video_id, object_key FROM video_ownership WHERE origin='upload'").fetchall()
        released = 0
        for row in rows:
            try:
                self.registry.get(row["video_id"])
                continue
            except DomainError:
                pass
            if self.identity.release_video(row["video_id"]):
                released += 1
                if row["object_key"]:
                    try:
                        self.store.delete(row["object_key"])
                    except ObjectStoreError:
                        pass
        return released


class _LeaseLost(Exception):
    pass
