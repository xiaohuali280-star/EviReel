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

def create_app(*, settings=None, backend=None, indexer=None, transcriber=None, reranker=None, study_model=None, chat_model=None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        service = IndexingService(settings, backend=backend, indexer=indexer, transcriber=transcriber, reranker=reranker)
        app.state.indexing = service
        await service.start()
        try:
            study = StudyService(service, model=study_model)
            app.state.study = study
            study.start()
            conversations = ConversationService(service, study=study, chat_model=chat_model)
            app.state.conversations = conversations
            conversations.start()
            service.on_indexed = study.auto
            yield
        finally:
            if hasattr(app.state, 'conversations'):
                await app.state.conversations.close()
            if hasattr(app.state, 'study'):
                await app.state.study.close()
            await service.close()

    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    app.add_middleware(UploadLimitMiddleware, max_bytes=settings.max_upload_bytes)
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
        return {'max_upload_bytes': settings.max_upload_bytes, 'max_video_duration_s': settings.max_video_duration_s,
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

    @app.get("/", include_in_schema=False)
    async def root_status():
        if (dist / 'index.html').is_file():
            return FileResponse(dist / 'index.html')
        return {"name": settings.app_name, "version": settings.app_version,
                "mode": "video-memory-api", "docs": "/docs", "websocket": "/ws/agent", "tui": "vam-tui"}
    return app

app = create_app()
