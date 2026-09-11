"""Cookie + password authentication for the student /api/v1 contract.

Bearer tokens remain valid for services. Browser students use HttpOnly cookies
and a CSRF token. User IDs in headers are never treated as identity.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .errors import CoreError
from .models import Principal

COOKIE = "cyberpod_session"
CSRF_HEADER = "X-CSRF-Token"
DEFAULT_USERS = {
    "demo@cyberpod.local": "CyberPodDemo123!",
}


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)


@dataclass
class BrowserSession:
    token: str
    subject: str
    email: str
    display_name: str
    csrf: str
    expires_at: datetime


class PasswordDirectory:
    def __init__(self, users: dict[str, str] | None = None):
        source = users or dict(DEFAULT_USERS)
        extra = os.environ.get("CYBERPOD_STUDENT_PASSWORD")
        if extra:
            source["demo@cyberpod.local"] = extra
        self._users = {}
        for email, password in source.items():
            salt = secrets.token_bytes(16)
            self._users[email.lower()] = (salt, _hash_password(password, salt))

    def verify(self, email: str, password: str) -> bool:
        record = self._users.get(email.lower())
        if record is None:
            _hash_password(password, b"0" * 16)
            return False
        salt, expected = record
        return hmac.compare_digest(expected, _hash_password(password, salt))


class BrowserSessions:
    def __init__(self, ttl_seconds: int = 8 * 3600):
        self.ttl = ttl_seconds
        self._sessions: dict[str, BrowserSession] = {}

    def create(self, email: str) -> BrowserSession:
        now = datetime.now(timezone.utc)
        session = BrowserSession(
            token=secrets.token_urlsafe(32),
            subject="demo-student" if email.lower() == "demo@cyberpod.local" else email.lower(),
            email=email.lower(),
            display_name="Demo Student" if email.lower() == "demo@cyberpod.local" else email.split("@")[0],
            csrf=secrets.token_urlsafe(24),
            expires_at=now + timedelta(seconds=self.ttl),
        )
        self._sessions[session.token] = session
        return session

    def get(self, token: str | None) -> BrowserSession | None:
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if datetime.now(timezone.utc) >= session.expires_at:
            self._sessions.pop(token, None)
            return None
        return session

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)


class StudentIdentity:
    """Password login for browsers; still accepts hashed bearer tokens for services."""

    def __init__(self, bearer, directory: PasswordDirectory | None = None,
                 sessions: BrowserSessions | None = None):
        self.bearer = bearer
        self.directory = directory or PasswordDirectory()
        self.sessions = sessions or BrowserSessions()

    async def authenticate(self, bearer_token: str):
        return await self.bearer.authenticate(bearer_token)

    def login(self, email: str, password: str) -> BrowserSession:
        if not email or not password or not self.directory.verify(email, password):
            raise CoreError("INVALID_CREDENTIALS", "Invalid email or password", 401)
        return self.sessions.create(email)

    def principal_from_cookie(self, token: str | None) -> Principal | None:
        session = self.sessions.get(token)
        if session is None:
            return None
        return Principal(subject=session.subject, scopes={"student"})

    def require_csrf(self, token: str | None, csrf: str | None, method: str) -> BrowserSession:
        session = self.sessions.get(token)
        if session is None:
            raise CoreError("AUTH_REQUIRED", "Valid session cookie required", 401)
        if method in {"GET", "HEAD", "OPTIONS"}:
            return session
        if not csrf or not hmac.compare_digest(session.csrf, csrf):
            raise CoreError("CSRF_INVALID", "CSRF token missing or invalid", 403)
        return session
