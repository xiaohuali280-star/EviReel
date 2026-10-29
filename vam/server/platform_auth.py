"""Platform-mode authentication and resource authorization (E1).

One ASGI middleware authenticates every /api request and authorizes every
path-addressed resource (video, job, conversation) against the caller's
workspaces before any existing route runs. Body-addressed resources
(conversation creation, WebSocket requests) call ``authorize_video`` directly.
In legacy mode (no DATABASE_URL) this module is inert.
"""
from __future__ import annotations

import re
from http.cookies import SimpleCookie

from starlette.responses import JSONResponse

from vam.runtime import run_sync
from vam.video_registry import DomainError

COOKIE = "evireel_session"
PUBLIC = {"/api/auth/login", "/api/ui-config", "/api/health"}
VIDEO_PATH = re.compile(r"^/api/videos/(v_[0-9a-f]{32})(?:/|$)")
JOB_PATH = re.compile(r"^/api/jobs/([0-9a-f]{32})(?:/|$)")
CONVERSATION_PATH = re.compile(r"^/api/conversations/(c_[0-9a-f]{32})(?:/|$)")


def credentials(scope) -> tuple[str | None, str | None]:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    token = None
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif "cookie" in headers:
        cookie = SimpleCookie()
        try:
            cookie.load(headers["cookie"])
        except Exception:
            cookie = {}
        if COOKIE in cookie:
            token = cookie[COOKIE].value
    return token, headers.get("x-evireel-workspace") or None


def _query_param(scope, name):
    from urllib.parse import parse_qs
    values = parse_qs(scope.get("query_string", b"").decode("latin-1")).get(name)
    return values[0] if values else None


def resolve_owner_video(app, path: str, method: str, scope) -> str | None:
    """Map a request path to the video whose ownership governs it (None = not video-scoped)."""
    if match := VIDEO_PATH.match(path):
        return match.group(1)
    registry = app.state.indexing.registry
    if match := JOB_PATH.match(path):
        pipeline = getattr(app.state, "pipeline", None)
        if pipeline is not None:   # worker mode: the PostgreSQL ledger is the truth for jobs
            try:
                return pipeline.repo.job(match.group(1))["video_id"]
            except DomainError:
                pass   # jobs from before worker mode exist only in the SQLite registry
        with registry.db.connect() as conn:
            row = conn.execute("SELECT video_id FROM index_jobs WHERE job_id=?", (match.group(1),)).fetchone()
        if row is None:
            raise DomainError(404, "job_not_found", "Job not found")
        return row[0]
    if match := CONVERSATION_PATH.match(path):
        with registry.db.connect() as conn:
            row = conn.execute("SELECT video_id FROM conversations WHERE conversation_id=?", (match.group(1),)).fetchone()
        if row is None:
            raise DomainError(404, "conversation_not_found", "Conversation not found")
        return row[0]
    if path.rstrip("/") == "/api/conversations" and method == "GET":
        video_id = _query_param(scope, "video_id")
        if not video_id:
            raise DomainError(422, "video_id_required", "video_id is required")
        return video_id
    return None


class PlatformAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        app = scope["app"]
        platform = getattr(app.state, "platform", None)
        if platform is None:
            return await self.app(scope, receive, send)
        path = scope["path"]
        if scope["type"] == "http" and (not path.startswith("/api/") or path in PUBLIC):
            return await self.app(scope, receive, send)
        token, workspace = credentials(scope)
        try:
            principal = await run_sync(platform.identity.authenticate, token, workspace)
            if scope["type"] == "http":
                if path.rstrip("/") == "/api/videos" and scope["method"] == "POST":
                    raise DomainError(410, "use_resumable_upload",
                                      "Single-request upload is disabled in platform mode; use /api/uploads")
                video_id = await run_sync(resolve_owner_video, app, path, scope["method"], scope)
                if video_id is not None:
                    await run_sync(platform.identity.authorize_video, principal, video_id)
        except DomainError as exc:
            if scope["type"] == "websocket":
                # Reject the handshake; the browser sees a failed connection (HTTP 403).
                return await send({"type": "websocket.close", "code": 4401})
            return await JSONResponse({"error": {"code": exc.code, "message": exc.message}},
                                      status_code=exc.status)(scope, receive, send)
        scope.setdefault("state", {})["principal"] = principal
        return await self.app(scope, receive, send)


def principal_of(conn_or_request):
    return conn_or_request.scope.get("state", {}).get("principal")


async def authorize_video(conn_or_request, video_id: str):
    """For body-addressed resources; no-op in legacy mode."""
    platform = getattr(conn_or_request.app.state, "platform", None)
    if platform is None:
        return
    principal = principal_of(conn_or_request)
    if principal is None:
        raise DomainError(401, "unauthenticated", "Sign in to continue")
    await run_sync(platform.identity.authorize_video, principal, video_id)
