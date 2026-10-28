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


class ObjectCleaner:
    def __init__(self, db, store):
        self.db, self.store = db, store

    def delete_now(self, object_key) -> bool:
        """Try one deletion of a queued key; on failure the row stays pending for drain()."""
        with self.db.connect() as conn:
            if conn.execute("SELECT 1 FROM video_ownership WHERE object_key=%s", (object_key,)).fetchone():
                # Never delete an object that some video still references.
                self._finish(object_key, "skipped", "still_referenced")
                return False
        try:
            self.store.delete(object_key)  # S3 DELETE is idempotent: a missing key also succeeds
        except ObjectStoreError as exc:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE object_deletions SET attempts=attempts+1, last_error=%s, next_attempt_at=now() + "
                    "make_interval(secs => LEAST(%s, 5 * power(2, LEAST(attempts, 12)))) "
                    "WHERE object_key=%s AND state='pending'", (str(exc)[:500] or type(exc).__name__, MAX_BACKOFF_S, object_key))
            logger.warning("object_delete_deferred key=%s error=%s", object_key, type(exc).__name__)
            return False
        self._finish(object_key, "done", None)
        return True

    def _finish(self, object_key, state, note):
        with self.db.transaction() as conn:
            conn.execute("UPDATE object_deletions SET state=%s, last_error=COALESCE(%s, last_error), attempts=attempts+1, "
                         "done_at=now() WHERE object_key=%s AND state='pending'", (state, note, object_key))

    def drain(self, limit=100) -> int:
        with self.db.connect() as conn:
            keys = [r["object_key"] for r in conn.execute(
                "SELECT object_key FROM object_deletions WHERE state='pending' AND next_attempt_at <= now() "
                "ORDER BY next_attempt_at LIMIT %s", (limit,))]
        return sum(1 for key in keys if self.delete_now(key))

    def pending(self) -> int:
        with self.db.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM object_deletions WHERE state='pending'").fetchone()["n"]
