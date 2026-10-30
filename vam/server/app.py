import asyncio
import contextlib
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from pathlib import Path
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from vam.config import get_settings
from vam.indexing.service import IndexingService
from vam.server.api.frames import router as frames_router
from vam.server.api.videos import router as videos_router
from vam.server.api.memory import router as memory_router
from vam.server.api.transcripts import router as transcripts_router
from vam.server.api.search import router as search_router
from vam.server.api.conversations import router as conversations_router
from vam.server.api.study import router as study_router
from vam.study import StudyService
from vam.conversation_service import ConversationService
from vam.server.ws.agent_chat import router as agent_chat_router
from vam.video_registry import DomainError
from vam.server.upload_limit import UploadLimitMiddleware
from vam.server.platform_auth import PlatformAuthMiddleware
from vam.server.api.auth import router as auth_router
from vam.server.api.uploads import router as uploads_router
from vam.runtime import run_sync

async def _sweep(app, service, gateway):
    """Coordination housekeeping on every instance (each step is idempotent and conditional):
    finish deletions whose readers are gone, remove retired generations past their grace period,
    turn expired model-usage reservations into 'unknown', purge long-expired lease rows."""
    import logging
    log = logging.getLogger("vam.server.sweep")
    settings = service.settings
    while True:
        await asyncio.sleep(settings.coordination_sweep_interval_s)
        try:
            result = await run_sync(service.sweep)
            if any(result.values()):
                log.info("coordination_sweep %s", result)
            await run_sync(gateway.ledger.reconcile)
            purge = getattr(service.coordination, "purge_expired", None)
            if purge is not None:
                await run_sync(purge)
        except Exception:
            log.exception("coordination_sweep_failed")


class InstanceHeaderMiddleware:
    """E3: names the API instance that answered (load-balancer diagnostics; not a security boundary).
    Plain ASGI, so streamed (Range) responses and their release callbacks are untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def tagged(message):
            if message["type"] == "http.response.start":
                instance = getattr(scope["app"].state, "instance_id", None) if "app" in scope else None
                if instance:
                    message = {**message, "headers": [*message.get("headers", []),
                                                      (b"x-evireel-instance", str(instance).encode())]}
            await send(message)
        await self.app(scope, receive, tagged)


def create_app(*, settings=None, backend=None, indexer=None, transcriber=None, reranker=None, study_model=None, chat_model=None,
               object_store=None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        from vam import gateway as model_gateway
        from vam.cache import LocalCache, RedisCache
        from vam.coordination import PgCoordinator, instance_id, redis_client
        from vam.events import LocalEventBus, RedisEventBus
        platform = None
        if settings.pipeline_mode == "worker" and not settings.platform_enabled:
            raise RuntimeError("PIPELINE_MODE=worker requires platform mode (DATABASE_URL and object storage)")
        if settings.api_multi_instance and not (settings.platform_enabled and settings.pipeline_mode == "worker"
                                                and settings.redis_url):
            # In-process indexing/notes recover at startup and cannot be shared; notifications and limits
            # must be shared. Refuse instead of silently running N independent instances.
            raise RuntimeError("API_MULTI_INSTANCE requires DATABASE_URL, PIPELINE_MODE=worker and REDIS_URL")
        if settings.api_multi_instance and settings.registry_backend != "postgres":
            # The read model (videos, jobs, conversations, notes) must not live in an instance-local file.
            raise RuntimeError("API_MULTI_INSTANCE requires REGISTRY_BACKEND=postgres")
        if settings.read_lease_max_s < settings.conversation_request_timeout_s + settings.read_lease_ttl_s:
            # A question's reads are protected for its whole deadline; the lease ending first would cancel
            # questions that are still allowed to run.
            raise RuntimeError("READ_LEASE_MAX_S must be at least CONVERSATION_REQUEST_TIMEOUT_S + READ_LEASE_TTL_S")
        if settings.model_limiter_on_redis_error == "local" and model_gateway.shares_model_limits(settings):
            # Other processes (API instances, pipeline workers) call models too: a per-process fallback cannot
            # keep their sum under the configured limits.
            raise RuntimeError("MODEL_LIMITER_ON_REDIS_ERROR=local is only allowed for a single API process "
                               "without pipeline workers; use reject")
        if settings.platform_enabled:
            from vam.platform import build_platform
            platform = await run_sync(build_platform, settings, store=object_store)
        instance = instance_id(settings)
        redis = redis_client(settings)
        app.state.instance_id, app.state.redis = instance, redis
        service = IndexingService(settings, backend=backend, indexer=indexer, transcriber=transcriber, reranker=reranker)
        app.state.indexing = service
        if platform is not None:
            # Leases/tombstones/retired generations in PostgreSQL: shared with other API instances and workers.
            service.coordination = PgCoordinator(platform.db, settings, instance)
        gateway = model_gateway.build_gateway(settings, redis_client=redis, db=platform.db if platform else None)
        model_gateway.configure(gateway)
        app.state.gateway = gateway
        app.state.cache = (RedisCache(redis, settings) if redis is not None else LocalCache(settings)) if settings.cache_enabled else None
        service.cache = app.state.cache
        if platform is not None:
            from vam.publication import PublicationCache, queue_unreferenced
            service.workspace_of = platform.identity.video_workspace
            if settings.publication_objects:
                service.publications = PublicationCache(service.registry, platform.store)
                service.queue_objects = lambda video_id, keys: queue_unreferenced(platform.db, keys, "video_deleted", video_id)
        events = RedisEventBus(redis, instance) if redis is not None else LocalEventBus()
        app.state.events = events
        if platform is not None and service.worker_mode:
            from vam.pipeline.api import PipelineClient
            service.pipeline = app.state.pipeline = PipelineClient(platform.db, service.registry, settings)
        await service.start()
        sweeper = None
        try:
            await service.coordination.start()
            await events.start()
            if platform is not None:
                from vam.platform.uploads import UploadService
                from vam.server.api.videos import release_platform_video
                platform.uploads = UploadService(platform.db, platform.store, platform.identity, service, settings)
                await platform.uploads.start()
                app.state.platform = platform
                service.on_reclaimed = lambda video_id: release_platform_video(platform, video_id)
            study = StudyService(service, model=study_model)
            app.state.study = study
            study.start()
            conversations = ConversationService(service, study=study, chat_model=chat_model, events=events, instance=instance)
            if platform is not None:
                conversations.workspace_of = platform.identity.video_workspace
                study.workspace_of = platform.identity.video_workspace
            app.state.conversations = conversations
            conversations.start()
            service.on_indexed = study.auto
            sweeper = asyncio.create_task(_sweep(app, service, gateway))
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper
            if hasattr(app.state, 'conversations'):
                await app.state.conversations.close()
            if hasattr(app.state, 'study'):
                await app.state.study.close()
            if platform is not None and platform.uploads is not None:
                await platform.uploads.close()
            await events.close()
            await service.coordination.close()
            model_gateway.configure(None)
            await service.close()

    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    app.add_middleware(UploadLimitMiddleware, max_bytes=settings.max_upload_bytes)
    app.add_middleware(InstanceHeaderMiddleware)
    # Added last = outermost: authentication runs before body limits and routing.
    app.add_middleware(PlatformAuthMiddleware)
    app.include_router(auth_router)
    app.include_router(uploads_router)
    app.include_router(videos_router)
    app.include_router(search_router)
    app.include_router(conversations_router)
    app.include_router(study_router)
    app.include_router(memory_router)
    app.include_router(transcripts_router)
    if settings.enable_legacy_http_api:
        app.include_router(frames_router)
    app.include_router(agent_chat_router)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.scope.get("state", {}).get("upload_limit_exceeded"):
            return JSONResponse(status_code=413, content={"error": {"code": "upload_limit", "message": "Upload exceeds configured size limit"}})
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "http_error", "message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": detail}, headers=exc.headers)

    @app.get('/api/ui-config')
    async def ui_config():
        from vam.indexing.media import ALLOWED_EXTENSIONS
        return {'upload_mode': 'resumable' if settings.platform_enabled else 'single',
                'auth_required': settings.platform_enabled,
                'max_upload_bytes': settings.max_upload_bytes, 'max_video_duration_s': settings.max_video_duration_s,
                'extensions': sorted(ALLOWED_EXTENSIONS), 'message_chars': settings.conversation_message_chars,
                'study_auto_generate': settings.study_auto_generate, 'model_configured': bool(settings.api_key or study_model)}

    dist = Path(settings.frontend_dist).expanduser().resolve() if settings.frontend_dist else Path(__file__).parent / 'static'
    if (dist / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='frontend-assets')

    @app.get('/app/{path:path}', include_in_schema=False)
    async def frontend(path: str):
        if (dist / 'index.html').is_file():
            return FileResponse(dist / 'index.html')
        raise HTTPException(503, detail={'code': 'frontend_not_built', 'message': 'Build frontend or set FRONTEND_DIST; see docs/P5_DEPLOYMENT.md'})

    @app.get('/api/health')
    async def health(request: Request):
        platform = getattr(request.app.state, 'platform', None)
        result = {'status': 'ok', 'mode': 'platform' if platform else 'single-user',
                  'instance': getattr(request.app.state, 'instance_id', None)}
        if platform is not None:
            try:
                await run_sync(platform.db.require_schema)
                await run_sync(platform.store.health)
            except Exception as exc:
                return JSONResponse(status_code=503, content={'status': 'degraded', 'error': type(exc).__name__})
        return result

    @app.get('/api/metrics')
    async def metrics(request: Request):
        """Per-instance counters for E3 diagnostics (authenticated in platform mode like every /api route)."""
        state = request.app.state
        events = state.events
        readers = state.indexing.readers
        return {'instance': state.instance_id, 'gateway': state.gateway.snapshot(),
                'cache': {'backend': state.cache.name, **state.cache.stats.as_dict()} if state.cache else None,
                'events': {'backend': 'redis' if events.shared else 'local',
                           'published': getattr(events, 'published', None), 'received': getattr(events, 'received', None),
                           'publish_errors': getattr(events, 'publish_errors', None)},
                'readers': {'open': len(readers.items), 'opens': readers.opens, 'hits': readers.hits},
                'coordination': 'postgresql' if state.indexing.coordination.shared else 'local',
                'running_questions': len(state.conversations.tasks)}

    @app.get("/", include_in_schema=False)
    async def root_status():
        if (dist / 'index.html').is_file():
            return FileResponse(dist / 'index.html')
        return {"name": settings.app_name, "version": settings.app_version,
                "mode": "video-memory-api", "docs": "/docs", "websocket": "/ws/agent", "tui": "vam-tui"}
    return app

app = create_app()
