from fastapi import APIRouter, Request, Response
from pydantic import Field

from vam.protocol import StrictSchemaModel
from vam.runtime import run_sync
from vam.server.platform_auth import COOKIE, credentials, principal_of
from vam.video_registry import DomainError

router = APIRouter(prefix="/api/auth", tags=["Auth"])


class Login(StrictSchemaModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _platform(request: Request):
    platform = getattr(request.app.state, "platform", None)
    if platform is None:
        raise DomainError(404, "auth_disabled", "Accounts are disabled in local single-user mode")
    return platform


@router.post("/login")
async def login(body: Login, request: Request, response: Response):
    platform = _platform(request)
    token, expires = await run_sync(platform.identity.login, body.username, body.password)
    settings = request.app.state.indexing.settings
    response.set_cookie(COOKIE, token, max_age=settings.auth_session_ttl_s, httponly=True,
                        secure=settings.auth_cookie_secure, samesite="lax", path="/")
    principal = await run_sync(platform.identity.authenticate, token, None)
    return await run_sync(platform.identity.me, principal)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    platform = _platform(request)
    token, _ = credentials(request.scope)
    if token:
        await run_sync(platform.identity.logout, token)
    response.delete_cookie(COOKIE, path="/")


@router.get("/me")
async def me(request: Request):
    platform = _platform(request)
    return await run_sync(platform.identity.me, principal_of(request))
