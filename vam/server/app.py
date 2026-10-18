from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from vam.config import get_settings
from vam.indexing.service import IndexingService
from vam.server.api.frames import router as frames_router
from vam.server.api.videos import router as videos_router
from vam.server.api.memory import router as memory_router
from vam.server.api.transcripts import router as transcripts_router
from vam.server.api.search import router as search_router
from vam.server.api.conversations import router as conversations_router
from vam.conversation_service import ConversationService
from vam.server.ws.agent_chat import router as agent_chat_router
from vam.video_registry import DomainError
from vam.server.upload_limit import UploadLimitMiddleware

def create_app(*, settings=None, backend=None, indexer=None, transcriber=None, reranker=None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        service = IndexingService(settings, backend=backend, indexer=indexer, transcriber=transcriber, reranker=reranker)
        app.state.indexing = service
        await service.start()
        try:
            conversations = ConversationService(service)
            app.state.conversations = conversations
            conversations.start()
            yield
        finally:
            if hasattr(app.state, 'conversations'):
                await app.state.conversations.close()
            await service.close()

    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    app.add_middleware(UploadLimitMiddleware, max_bytes=settings.max_upload_bytes)
    app.include_router(videos_router)
    app.include_router(search_router)
    app.include_router(conversations_router)
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

    @app.get("/", include_in_schema=False)
    async def root_status():
        return {"name": settings.app_name, "version": settings.app_version,
                "mode": "video-memory-api", "docs": "/docs", "websocket": "/ws/agent", "tui": "vam-tui"}
    return app

app = create_app()
