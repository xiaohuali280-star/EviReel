"""Durable deletion of objects that no longer back any video (E1).

``enqueue`` runs inside the caller's transaction, together with the state change that made
the object unreachable. ``delete_now`` is the fast path right after commit; ``drain`` is run
by the upload reconciler and retries with backoff until storage confirms the deletion.
Quota is released by the logical transition (ownership removed / upload failed), not by the
physical deletion: pending rows are storage debt the operator can see, not user quota.
"""
from __future__ import annotations

import logging

from vam.platform.object_store import ObjectStoreError

logger = logging.getLogger(__name__)

MAX_BACKOFF_S = 3600


def enqueue(conn, object_key, reason, *, video_id=None, upload_id=None):
    conn.execute(
        "INSERT INTO object_deletions(object_key, reason, video_id, upload_id) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (object_key) DO UPDATE SET state='pending', done_at=NULL, next_attempt_at=now()",
        (object_key, reason, video_id, upload_id))


def lock_key(conn, object_key):
    """Per-object mutual exclusion (transaction-scoped): new references (checkpoint registration),
    deletion intents (sweep enqueue) and the final delete of one key are serialized by this lock."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 7031))", (object_key,))


def referenced(conn, object_key) -> str | None:
    """Why an object must not be deleted, or None. Checked under lock_key, so it cannot change before
    the delete completes (checkpoint registration takes the same lock and refuses queued keys)."""
    if conn.execute("SELECT 1 FROM video_ownership WHERE object_key=%s", (object_key,)).fetchone():
        return "video_ownership"
    if conn.execute("SELECT 1 FROM checkpoints WHERE object_key=%s", (object_key,)).fetchone():
        return "checkpoint"
    if conn.execute("SELECT 1 FROM job_stages WHERE output IS NOT NULL AND strpos(output::text, %s) > 0 LIMIT 1",
                    (f'"{object_key}"',)).fetchone():
        return "stage_output"
    return None


def deletion_blocked(conn, object_key) -> bool:
    """For new references: a key that is queued for deletion (or already deleted) must not be reused."""
    return bool(conn.execute("SELECT 1 FROM object_deletions WHERE object_key=%s AND state IN ('pending','done')",
                             (object_key,)).fetchone())


class ObjectCleaner:
    def __init__(self, db, store):
        self.db, self.store = db, store

    def delete_now(self, object_key) -> bool:
        """Try one deletion of a queued key; on failure the row stays pending for drain().

        Runs entirely under the key's lock: re-check that nothing references the object, delete it,
        record the outcome. A reference that appears later is impossible (registration of a queued
        key is refused) and one that appeared earlier is seen here, so the row is skipped instead."""
        with self.db.transaction() as conn:
            lock_key(conn, object_key)
            row = conn.execute("SELECT state FROM object_deletions WHERE object_key=%s FOR UPDATE", (object_key,)).fetchone()
            if row is None or row["state"] != "pending":
                return False
            reason = referenced(conn, object_key)
            if reason:
                note = "still_referenced" if reason == "video_ownership" else f"still_referenced:{reason}"
                conn.execute("UPDATE object_deletions SET state='skipped', last_error=%s, attempts=attempts+1, done_at=now() "
                             "WHERE object_key=%s", (note, object_key))
                return False
            try:
                self.store.delete(object_key)  # S3 DELETE is idempotent: a missing key also succeeds
            except ObjectStoreError as exc:
                conn.execute(
                    "UPDATE object_deletions SET attempts=attempts+1, last_error=%s, next_attempt_at=now() + "
                    "make_interval(secs => LEAST(%s, 5 * power(2, LEAST(attempts, 12)))) WHERE object_key=%s",
                    (str(exc)[:500] or type(exc).__name__, MAX_BACKOFF_S, object_key))
                logger.warning("object_delete_deferred key=%s error=%s", object_key, type(exc).__name__)
                return False
            conn.execute("UPDATE object_deletions SET state='done', attempts=attempts+1, done_at=now() WHERE object_key=%s",
                         (object_key,))
            return True

    def drain(self, limit=100) -> int:
        with self.db.connect() as conn:
            keys = [r["object_key"] for r in conn.execute(
                "SELECT object_key FROM object_deletions WHERE state='pending' AND next_attempt_at <= now() "
                "ORDER BY next_attempt_at LIMIT %s", (limit,))]
        return sum(1 for key in keys if self.delete_now(key))

    def pending(self) -> int:
        with self.db.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM object_deletions WHERE state='pending'").fetchone()["n"]
