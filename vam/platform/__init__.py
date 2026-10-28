"""E1 platform layer: PostgreSQL metadata, identity/ownership and object storage.

Data boundary for E1 (see docs/engineering/E1_DELIVERY.md):
* PostgreSQL (this package): users, workspaces, memberships, auth sessions,
  video ownership, storage quota, upload sessions/parts.
* SQLite registry (vam.storage): videos, index_jobs, conversations, study -
  unchanged; no table is written in both stores.
* Object storage: verified originals of resumable uploads. The per-video
  working copy and indexes under DATA_ROOT stay local until E2.
"""
from __future__ import annotations

from dataclasses import dataclass

from vam.platform.db import PlatformDB
from vam.platform.identity import IdentityService
from vam.platform.object_store import S3ObjectStore


@dataclass
class Platform:
    db: PlatformDB
    identity: IdentityService
    store: object
    uploads: object = None


def build_platform(settings, *, store=None) -> Platform:
    missing = [name for name in ("s3_endpoint", "s3_access_key", "s3_secret_key", "s3_bucket")
               if not getattr(settings, name)] if store is None else []
    if missing:
        raise RuntimeError("Platform mode (DATABASE_URL set) also requires: " + ", ".join(m.upper() for m in missing))
    if settings.enable_legacy_http_api:
        raise RuntimeError("ENABLE_LEGACY_HTTP_API is not authorized per workspace; disable it in platform mode")
    db = PlatformDB(settings.database_url)
    db.require_schema()
    store = store or S3ObjectStore(endpoint=settings.s3_endpoint, public_endpoint=settings.s3_public_endpoint,
                                   access_key=settings.s3_access_key, secret_key=settings.s3_secret_key,
                                   bucket=settings.s3_bucket, region=settings.s3_region)
    return Platform(db=db, identity=IdentityService(db, settings), store=store)
