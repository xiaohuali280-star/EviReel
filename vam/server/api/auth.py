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


class Register(StrictSchemaModel):
    # Rules (username pattern, password length) are checked by IdentityService.register with specific codes.
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


async def _signed_in(platform, request: Request, response: Response, token: str):
    settings = request.app.state.indexing.settings
    response.set_cookie(COOKIE, token, max_age=settings.auth_session_ttl_s, httponly=True,
                        secure=settings.auth_cookie_secure, samesite="lax", path="/")
    principal = await run_sync(platform.identity.authenticate, token, None)
    return await run_sync(platform.identity.me, principal)


@router.post("/login")
async def login(body: Login, request: Request, response: Response):
    platform = _platform(request)
    token, _ = await run_sync(platform.identity.login, body.username, body.password)
    return await _signed_in(platform, request, response, token)


@router.post("/register", status_code=201)
async def register(body: Register, request: Request, response: Response):
    platform = _platform(request)
    if not request.app.state.indexing.settings.auth_allow_registration:
        raise DomainError(403, "registration_disabled", "Self-service sign-up is disabled; ask an administrator for an account")
    token, _ = await run_sync(platform.identity.register, body.username, body.password)
    return await _signed_in(platform, request, response, token)


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
