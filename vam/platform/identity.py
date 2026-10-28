"""Users, workspaces, server-side sessions and resource ownership.

Identity always comes from a server-verified session token (HttpOnly cookie or
Bearer header). Request bodies never carry a trusted user_id/workspace_id.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from vam.video_registry import DomainError

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


def hash_password(password: str) -> str:
    if not 10 <= len(password) <= 256:
        raise ValueError("password must be 10-256 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(SCRYPT_N, SCRYPT_R, SCRYPT_P,
                                          base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(base64.b64decode(digest)))
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


# A fixed hash used to equalize timing when the username does not exist.
_DUMMY_HASH = None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    workspace_id: str          # active workspace for this request
    workspaces: frozenset      # all workspace_ids the user is a member of


class IdentityService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    # -- administration (CLI / tests) --------------------------------------
    def create_user(self, username: str, password: str, *, quota_bytes: int | None = None,
                    workspace_name: str | None = None):
        if not USERNAME.fullmatch(username):
            raise ValueError("username must match [A-Za-z0-9_.-]{3,64}")
        user_id, workspace_id = "usr_" + uuid.uuid4().hex, "ws_" + uuid.uuid4().hex
        quota = self.settings.default_workspace_quota_bytes if quota_bytes is None else quota_bytes
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM users WHERE username=%s", (username,)).fetchone():
                raise ValueError("username already exists")
            conn.execute("INSERT INTO users(user_id, username, password_hash) VALUES (%s,%s,%s)",
                         (user_id, username, hash_password(password)))
            conn.execute("INSERT INTO workspaces(workspace_id, name, quota_bytes) VALUES (%s,%s,%s)",
                         (workspace_id, workspace_name or f"{username} 的学习空间", quota))
            conn.execute("INSERT INTO memberships(workspace_id, user_id, role, is_default) VALUES (%s,%s,'owner',true)",
                         (workspace_id, user_id))
        return {"user_id": user_id, "workspace_id": workspace_id, "username": username}

    def add_member(self, workspace_id: str, user_id: str, role: str = "member"):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO memberships(workspace_id, user_id, role) VALUES (%s,%s,%s) "
                         "ON CONFLICT DO NOTHING", (workspace_id, user_id, role))

    def set_quota(self, workspace_id: str, quota_bytes: int):
        with self.db.transaction() as conn:
            row = conn.execute("UPDATE workspaces SET quota_bytes=%s WHERE workspace_id=%s RETURNING workspace_id",
                               (quota_bytes, workspace_id)).fetchone()
        if not row:
            raise ValueError("workspace not found")

    # -- sessions ------------------------------------------------------------
    def login(self, username: str, password: str):
        global _DUMMY_HASH
        with self.db.connect() as conn:
            user = conn.execute("SELECT user_id, password_hash, disabled FROM users WHERE username=%s",
                                (username,)).fetchone()
        if user is None:
            _DUMMY_HASH = _DUMMY_HASH or hash_password("timing-equalizer-password")
            verify_password(password, _DUMMY_HASH)
            raise DomainError(401, "invalid_credentials", "Invalid username or password")
        if user["disabled"] or not verify_password(password, user["password_hash"]):
            raise DomainError(401, "invalid_credentials", "Invalid username or password")
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.settings.auth_session_ttl_s)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO auth_sessions(token_hash, user_id, expires_at) VALUES (%s,%s,%s)",
                         (token_hash(token), user["user_id"], expires))
        return token, expires

    def logout(self, token: str):
        with self.db.transaction() as conn:
            conn.execute("UPDATE auth_sessions SET revoked_at=now() WHERE token_hash=%s AND revoked_at IS NULL",
                         (token_hash(token),))

    def authenticate(self, token: str | None, requested_workspace: str | None = None) -> Principal:
        if not token or len(token) > 256:
            raise DomainError(401, "unauthenticated", "Sign in to continue")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT u.user_id, u.username FROM auth_sessions s JOIN users u USING (user_id) "
                "WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at > now() AND NOT u.disabled",
                (token_hash(token),)).fetchone()
            if row is None:
                raise DomainError(401, "unauthenticated", "Session expired; sign in again")
            members = conn.execute("SELECT workspace_id, is_default FROM memberships WHERE user_id=%s",
                                   (row["user_id"],)).fetchall()
        workspaces = frozenset(m["workspace_id"] for m in members)
        if requested_workspace:
            if requested_workspace not in workspaces:
                raise DomainError(404, "workspace_not_found", "Workspace not found")
            active = requested_workspace
        else:
            active = next((m["workspace_id"] for m in members if m["is_default"]), None) or min(workspaces, default=None)
        if active is None:
            raise DomainError(403, "no_workspace", "User has no workspace")
        return Principal(row["user_id"], row["username"], active, workspaces)

    def me(self, principal: Principal):
        with self.db.connect() as conn:
            ws = conn.execute("SELECT workspace_id, name, quota_bytes, used_bytes, reserved_bytes FROM workspaces "
                              "WHERE workspace_id=%s", (principal.workspace_id,)).fetchone()
        return {"user_id": principal.user_id, "username": principal.username, "workspace": dict(ws)}

    # -- ownership -------------------------------------------------------------
    def owned_video_ids(self, workspace_id: str) -> list[str]:
        with self.db.connect() as conn:
            return [r["video_id"] for r in conn.execute(
                "SELECT video_id FROM video_ownership WHERE workspace_id=%s", (workspace_id,))]

    def video_workspace(self, video_id: str) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT workspace_id FROM video_ownership WHERE video_id=%s", (video_id,)).fetchone()
        return row["workspace_id"] if row else None

    def authorize_video(self, principal: Principal, video_id: str):
        """Unknown and foreign videos are indistinguishable (404) to avoid an existence oracle."""
        if self.video_workspace(video_id) not in principal.workspaces:
            raise DomainError(404, "video_not_found", "Video not found")

    def release_video(self, video_id: str):
        """Drop ownership after the SQLite video row was deleted; idempotent."""
        with self.db.transaction() as conn:
            row = conn.execute("DELETE FROM video_ownership WHERE video_id=%s RETURNING workspace_id, size_bytes, object_key",
                               (video_id,)).fetchone()
            if row:
                conn.execute("UPDATE workspaces SET used_bytes=GREATEST(used_bytes-%s, 0) WHERE workspace_id=%s",
                             (row["size_bytes"], row["workspace_id"]))
        return dict(row) if row else None
