"""Platform migration and administration CLI (E1).

    python -m vam.platform.migrate schema [--dry-run]
    python -m vam.platform.migrate create-user --username alice [--quota-gib 20]   # password from EVIREEL_NEW_PASSWORD or prompt
    python -m vam.platform.migrate set-quota --workspace ws_... --quota-gib 50
    python -m vam.platform.migrate backfill-legacy --data-root data/evireel --username alice [--dry-run]
    python -m vam.platform.migrate check --data-root data/evireel
    python -m vam.platform.migrate rollback-legacy --workspace ws_... [--dry-run]

The legacy SQLite registry is opened read-only; this tool never modifies or moves
existing video files or SQLite rows. Backfill only adds ``video_ownership`` rows
(origin='legacy') so existing videos become visible to one workspace; rollback
removes exactly those rows. Validate on a copy of DATA_ROOT and a scratch
database first (see docs/engineering/E1_DELIVERY.md).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys
from pathlib import Path

GiB = 1024 ** 3


def read_legacy_videos(data_root: str):
    path = Path(data_root).expanduser().resolve() / "registry.sqlite3"
    if not path.is_file():
        raise SystemExit(f"registry not found: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        rows = conn.execute("SELECT video_id, size_bytes, status, trashed_at FROM videos ORDER BY created_at").fetchall()
    finally:
        conn.close()
    return version, [{"video_id": r[0], "size_bytes": int(r[1]), "status": r[2], "trashed": r[3] is not None} for r in rows]


def _workspace(db, *, workspace=None, username=None):
    with db.connect() as conn:
        if workspace:
            row = conn.execute("SELECT * FROM workspaces WHERE workspace_id=%s", (workspace,)).fetchone()
        else:
            row = conn.execute("SELECT w.* FROM workspaces w JOIN memberships m USING (workspace_id) JOIN users u USING (user_id) "
                               "WHERE u.username=%s AND m.is_default", (username,)).fetchone()
    if row is None:
        raise SystemExit("workspace not found")
    return row


def backfill_legacy(db, data_root, workspace_id, *, dry_run):
    version, videos = read_legacy_videos(data_root)
    with db.connect() as conn:
        owned = {r["video_id"]: r["workspace_id"] for r in conn.execute("SELECT video_id, workspace_id FROM video_ownership")}
    plan = [v for v in videos if v["video_id"] not in owned]
    report = {"registry_schema": version, "legacy_videos": len(videos), "already_owned": len(videos) - len(plan),
              "owned_by_other_workspace": sum(1 for v in videos if owned.get(v["video_id"]) not in (None, workspace_id)),
              "to_assign": len(plan), "bytes": sum(v["size_bytes"] for v in plan), "dry_run": dry_run,
              "workspace_id": workspace_id}
    if dry_run or not plan:
        return report
    with db.transaction() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE workspace_id=%s FOR UPDATE", (workspace_id,)).fetchone()
        inserted = 0
        for v in plan:
            inserted += conn.execute("INSERT INTO video_ownership(video_id, workspace_id, origin, size_bytes) "
                                     "VALUES (%s,%s,'legacy',%s) ON CONFLICT (video_id) DO NOTHING RETURNING 1",
                                     (v["video_id"], workspace_id, v["size_bytes"])).rowcount or 0
        total = conn.execute("SELECT COALESCE(SUM(size_bytes),0) AS s FROM video_ownership WHERE workspace_id=%s AND origin='legacy'",
                             (workspace_id,)).fetchone()["s"]
        conn.execute("UPDATE workspaces SET used_bytes=(SELECT COALESCE(SUM(size_bytes),0) FROM video_ownership "
                     "WHERE workspace_id=%s) WHERE workspace_id=%s", (workspace_id, workspace_id))
        used = conn.execute("SELECT used_bytes FROM workspaces WHERE workspace_id=%s", (workspace_id,)).fetchone()["used_bytes"]
    report |= {"inserted": inserted, "legacy_bytes_in_workspace": int(total), "workspace_used_bytes": int(used),
               "over_quota": int(used) + int(ws["reserved_bytes"]) > int(ws["quota_bytes"])}
    return report


def rollback_legacy(db, workspace_id, *, dry_run):
    with db.connect() as conn:
        rows = conn.execute("SELECT video_id, size_bytes FROM video_ownership WHERE workspace_id=%s AND origin='legacy'",
                            (workspace_id,)).fetchall()
    report = {"workspace_id": workspace_id, "legacy_rows": len(rows), "bytes": sum(r["size_bytes"] for r in rows), "dry_run": dry_run}
    if dry_run or not rows:
        return report
    with db.transaction() as conn:
        conn.execute("SELECT 1 FROM workspaces WHERE workspace_id=%s FOR UPDATE", (workspace_id,))
        deleted = conn.execute("DELETE FROM video_ownership WHERE workspace_id=%s AND origin='legacy'", (workspace_id,)).rowcount
        conn.execute("UPDATE workspaces SET used_bytes=(SELECT COALESCE(SUM(size_bytes),0) FROM video_ownership "
                     "WHERE workspace_id=%s) WHERE workspace_id=%s", (workspace_id, workspace_id))
    return report | {"deleted": deleted}


def check(db, data_root=None):
    issues = []
    with db.connect() as conn:
        quota = conn.execute(
            "SELECT w.workspace_id, w.used_bytes, w.reserved_bytes, "
            "(SELECT COALESCE(SUM(size_bytes),0) FROM video_ownership o WHERE o.workspace_id=w.workspace_id) AS owned, "
            "(SELECT COALESCE(SUM(declared_size),0) FROM upload_sessions s WHERE s.workspace_id=w.workspace_id "
            " AND s.quota_state='reserved') AS reserving FROM workspaces w").fetchall()
        for r in quota:
            if r["used_bytes"] != r["owned"]:
                issues.append({"code": "used_bytes_drift", "workspace_id": r["workspace_id"], "recorded": r["used_bytes"], "actual": int(r["owned"])})
            if r["reserved_bytes"] != r["reserving"]:
                issues.append({"code": "reserved_bytes_drift", "workspace_id": r["workspace_id"], "recorded": r["reserved_bytes"], "actual": int(r["reserving"])})
        owned = {r["video_id"] for r in conn.execute("SELECT video_id FROM video_ownership")}
        stuck = conn.execute("SELECT upload_id, state FROM upload_sessions WHERE state IN ('initiated','completing','verifying') "
                             "AND lease_until < now() - interval '10 minutes'").fetchall()
        deletions = conn.execute("SELECT COUNT(*) AS n, MIN(requested_at) AS oldest, MAX(attempts) AS attempts "
                                 "FROM object_deletions WHERE state='pending'").fetchone()
    issues += [{"code": "stuck_upload", **dict(r)} for r in stuck]
    if deletions["n"]:  # storage not yet reclaimed; the reconciler keeps retrying
        issues.append({"code": "pending_object_deletions", "count": deletions["n"],
                       "oldest": deletions["oldest"].isoformat(), "max_attempts": deletions["attempts"]})
    report = {"workspaces": len(quota), "owned_videos": len(owned)}
    if data_root:
        _, videos = read_legacy_videos(data_root)
        ids = {v["video_id"] for v in videos}
        report |= {"registry_videos": len(ids), "unowned_registry_videos": sorted(ids - owned)[:50],
                   "unowned_count": len(ids - owned), "ownership_without_registry": sorted(owned - ids)[:50]}
        if owned - ids:
            issues.append({"code": "ownership_without_registry", "count": len(owned - ids)})
    return report | {"issues": issues, "ok": not issues}


def main(argv=None):
    from vam.config import Settings
    from vam.platform.db import PlatformDB, redact_url
    from vam.platform.identity import IdentityService

    parser = argparse.ArgumentParser(prog="python -m vam.platform.migrate")
    parser.add_argument("--database-url", default=None, help="defaults to DATABASE_URL from env/.env")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("schema"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("create-user"); p.add_argument("--username", required=True); p.add_argument("--quota-gib", type=float)
    p = sub.add_parser("set-quota"); p.add_argument("--workspace", required=True); p.add_argument("--quota-gib", type=float, required=True)
    p = sub.add_parser("backfill-legacy"); p.add_argument("--data-root", required=True)
    g = p.add_mutually_exclusive_group(required=True); g.add_argument("--workspace"); g.add_argument("--username")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("check"); p.add_argument("--data-root")
    p = sub.add_parser("rollback-legacy"); p.add_argument("--workspace", required=True); p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings()
    url = args.database_url or settings.database_url
    if not url:
        raise SystemExit("DATABASE_URL is not configured")
    db = PlatformDB(url)
    print(f"# database: {redact_url(url)}", file=sys.stderr)
    if args.command == "schema":
        before = db.current_version()
        result = {"version_before": before, ("pending" if args.dry_run else "applied"): db.migrate(dry_run=args.dry_run),
                  "version_after": db.current_version()}
    else:
        db.require_schema()
        if args.command == "create-user":
            password = os.environ.get("EVIREEL_NEW_PASSWORD") or getpass.getpass("New password (10+ chars): ")
            quota = int(args.quota_gib * GiB) if args.quota_gib is not None else None
            result = IdentityService(db, settings).create_user(args.username, password, quota_bytes=quota)
        elif args.command == "set-quota":
            IdentityService(db, settings).set_quota(args.workspace, int(args.quota_gib * GiB))
            result = {"workspace_id": args.workspace, "quota_bytes": int(args.quota_gib * GiB)}
        elif args.command == "backfill-legacy":
            ws = _workspace(db, workspace=args.workspace, username=args.username)
            result = backfill_legacy(db, args.data_root, ws["workspace_id"], dry_run=args.dry_run)
        elif args.command == "check":
            result = check(db, args.data_root)
        else:
            result = rollback_legacy(db, args.workspace, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
