"""Platform read-side wiring shared by the API process and offline readers (the E4 evaluation runner).

``assemble`` gives an IndexingService the same protections the API has (E3):
* PostgreSQL read leases / tombstones / retired generations (``PgCoordinator``) in platform mode, so a reader
  in another process keeps its generation from being swept;
* the model gateway with the shared Redis rate windows and the persistent PostgreSQL usage ledger;
* the query-vector cache (Redis or in-process);
* the owning workspace of each video (model budget and cache scope) and object restore of published index
  files missing from DATA_ROOT.
The caller configures the returned gateway as the process gateway, starts/closes ``service.coordination``
and owns everything else (event bus, conversations, uploads).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Runtime:
    platform: object | None
    redis: object | None
    gateway: object
    instance: str

    def describe(self, service) -> dict:
        """What is wired (for evaluation metadata and diagnostics); no secrets."""
        ledger = self.gateway.ledger
        inner = getattr(ledger, 'inner', None)   # the evaluation ledger wraps the platform ledger
        return {'platform': self.platform is not None, 'coordination': type(service.coordination).__name__,
                'limiter': type(self.gateway.limiter).__name__, 'ledger': type(inner or ledger).__name__,
                'ledger_persistent': bool(getattr(inner or ledger, 'persistent', False)),
                'evaluation_ledger_on_top': inner is not None,
                'cache': type(service.cache).__name__ if service.cache is not None else None,
                'workspace_of': service.workspace_of is not None, 'publications': service.publications is not None}


def assemble(settings, service, *, object_store=None, instance=None) -> Runtime:
    """Blocking (opens database connections): call off the event loop."""
    from vam import gateway as model_gateway
    from vam.cache import LocalCache, RedisCache
    from vam.coordination import PgCoordinator, instance_id, redis_client
    platform = None
    if settings.platform_enabled:
        from vam.platform import build_platform
        platform = build_platform(settings, store=object_store)
    instance = instance or instance_id(settings)
    redis = redis_client(settings)
    if platform is not None:
        # Leases/tombstones/retired generations in PostgreSQL: shared with other API instances and workers.
        service.coordination = PgCoordinator(platform.db, settings, instance)
    gateway = model_gateway.build_gateway(settings, redis_client=redis, db=platform.db if platform else None)
    service.cache = (RedisCache(redis, settings) if redis is not None else LocalCache(settings)) if settings.cache_enabled else None
    if platform is not None:
        from vam.publication import PublicationCache, queue_unreferenced
        service.workspace_of = platform.identity.video_workspace
        if settings.publication_objects:
            service.publications = PublicationCache(service.registry, platform.store)
            service.queue_objects = lambda video_id, keys: queue_unreferenced(platform.db, keys, "video_deleted", video_id)
    return Runtime(platform, redis, gateway, instance)
