"""Orphan reconciliation for pipeline products in object storage.

Products are written before the ledger records them (upload -> checkpoint row -> stage commit), so a
crash or a lost checkpoint race leaves objects nothing points to. This sweep deletes an object under
the pipeline prefix only when ALL hold:
  * older than PIPELINE_ORPHAN_GRACE_S (an attempt that is still uploading/registering is protected);
  * not referenced by any checkpoint row (reusable products);
  * not referenced by any stage output (the products of every committed stage);
  * not produced by an attempt whose stage is still running (its token is part of the key).
Deletion goes through the durable queue (vam.platform.cleanup), so a storage failure retries.
"""
from __future__ import annotations

import re
import time

from vam.platform.cleanup import ObjectCleaner, enqueue

TOKEN = re.compile(r"/([0-9a-f]{32})/[^/]+$")


def referenced_keys(db) -> tuple[set, set]:
    with db.connect() as conn:
        keys = {r["object_key"] for r in conn.execute("SELECT object_key FROM checkpoints")}
        for row in conn.execute("SELECT output::text AS output FROM job_stages WHERE output IS NOT NULL"):
            keys.update(re.findall(r'"key":\s*"([^"]+)"', row["output"]))
        running = {r["attempt_token"] for r in conn.execute(
            "SELECT attempt_token FROM job_stages WHERE status='running' AND attempt_token IS NOT NULL")}
    return keys, running


def sweep_orphans(db, store, prefix, grace_s, now=None) -> dict:
    now = time.time() if now is None else now
    listed = store.list_objects(prefix)
    keys, running = referenced_keys(db)   # read AFTER listing: anything committed meanwhile is protected
    orphans = []
    for key, modified, _ in listed:
        if now - modified < grace_s or key in keys:
            continue
        token = TOKEN.search(key)
        if token and token.group(1) in running:
            continue
        orphans.append(key)
    if orphans:
        with db.transaction() as conn:
            for key in orphans:
                enqueue(conn, key, "pipeline_orphan")
        cleaner = ObjectCleaner(db, store)
        for key in orphans:
            cleaner.delete_now(key)
    return {"listed": len(listed), "orphans": len(orphans)}
