"""Grubhub HTTP client with authentication and header management."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import uuid4

import httpx

try:
    import fcntl
except ImportError:  # Windows: refreshes are still serialized within each client.
    fcntl = None

logger = logging.getLogger(__name__)

BASE_URL = "https://api-gtm.grubhub.com"
API_KEY = "ghandroid_Ujtwar5s9e3RYiSNV31X41y2hsK6Kh1Uv7JDrkpS"

_SESSION_DIR = Path(os.environ.get("GRUBHUB_SESSION_DIR", Path.home() / ".grubhub-mcp"))
_SESSION_FILE = _SESSION_DIR / "session.json"

_PATH_ID = re.compile(r"[A-Za-z0-9_-]+")


def path_id(value: str) -> str:
    """Return an ID that is safe to embed as a single URL path segment.

    IDs come from the model, so anything that could add path segments, a query
    or a fragment ("/", "..", "?", "#", "%") is rejected rather than escaped.
    """
    if not _PATH_ID.fullmatch(value):
        raise ValueError(f"Invalid Grubhub ID {value!r}: use only letters, digits, '-' and '_'")
    return value


class GrubhubSession:
    """Authentication state persisted across stdio server invocations."""

    def __init__(self) -> None:
        self.auth_token: str | None = None
        self.refresh_token: str | None = None
        self.diner_udid: str | None = None
        self.browser_id: str = str(uuid4())
        self.is_authenticated: bool = False
        self.session_handle: dict[str, Any] | None = None
        self.csrf_token: str | None = None
        self.login_generation: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            if _SESSION_FILE.exists():
                data = json.loads(_SESSION_FILE.read_text())
                if not isinstance(data, dict):
                    raise ValueError("Invalid session file")
                self.auth_token = data.get("auth_token")
                self.refresh_token = data.get("refresh_token")
                self.diner_udid = data.get("diner_udid")
                self.browser_id = data.get("browser_id", self.browser_id)
                self.is_authenticated = data.get("is_authenticated", False)
                self.session_handle = data.get("session_handle")
                self.csrf_token = data.get("csrf_token")
                self.login_generation = data.get("login_generation")
        except (OSError, ValueError, TypeError):
            logger.debug("Failed to load persisted session", exc_info=True)

    def _save(self) -> None:
        """Atomically replace the session, with private permissions from creation."""
        temporary: str | None = None
        try:
            _SESSION_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".session-", dir=_SESSION_DIR)
            with os.fdopen(fd, "w") as stream:
                json.dump({
                    "auth_token": self.auth_token,
                    "refresh_token": self.refresh_token,
                    "diner_udid": self.diner_udid,
                    "browser_id": self.browser_id,
                    "is_authenticated": self.is_authenticated,
                    "session_handle": self.session_handle,
                    "csrf_token": self.csrf_token,
                    "login_generation": self.login_generation,
                }, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, _SESSION_FILE)
        except OSError:
            logger.warning("Failed to persist session; next invocation may require login")
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _set_tokens(self, data: dict[str, Any], *, preserve: bool = False) -> None:
        handle = data.get("session_handle") or {}
        merged = dict(self.session_handle or {}) if preserve else {}
        # A rotated token with no creation timestamp must not inherit stale expiry.
        for token, timing in (
            ("access_token", ("token_created", "token_created_time", "token_expire_time")),
            ("refresh_token", ("refresh_token_created", "refresh_token_created_time", "refresh_token_expire_time")),
        ):
            value = handle.get(token) or (data.get("auth_token") if token == "access_token" else None)
            if value and value != merged.get(token):
                for key in timing:
                    if key not in handle:
                        merged.pop(key, None)
        merged.update(handle)
        self.auth_token = handle.get("access_token") or data.get("auth_token") or (
            self.auth_token if preserve else None
        )
        self.refresh_token = handle.get("refresh_token") or (
            self.refresh_token if preserve else None
        )
        # Null/omitted refresh tokens do not erase a still-valid previous token.
        if self.auth_token:
            merged["access_token"] = self.auth_token
        if self.refresh_token:
            merged["refresh_token"] = self.refresh_token
        self.session_handle = merged

    def set_authenticated(self, session_data: dict[str, Any]) -> None:
        """Start a new login; never carry another account's identity forward."""
        self._set_tokens(session_data)
        credential = session_data.get("credential") or {}
        self.diner_udid = credential.get("ud_id") or credential.get("udid")
        self.is_authenticated = True
        self.csrf_token = None
        self.login_generation = str(uuid4())
        self._save()

    def update_tokens(self, session_data: dict[str, Any]) -> None:
        """Merge refresh/session metadata without forgetting login identity."""
        self._set_tokens(session_data, preserve=True)
        credential = session_data.get("credential") or {}
        self.diner_udid = credential.get("ud_id") or credential.get("udid") or self.diner_udid
        self._save()

    def set_anonymous(self, session_data: dict[str, Any]) -> None:
        self._set_tokens(session_data)
        self.diner_udid = None
        self.csrf_token = None
        self.is_authenticated = False
        self.login_generation = str(uuid4())
        self._save()

    def access_token_expiring(self) -> bool:
        """Unknown expiry metadata falls back to the authenticated 401 retry."""
        handle = self.session_handle or {}
        try:
            expires_millis = float(handle.get("token_expire_time") or 0)
            if expires_millis > 0:
                expires = datetime.fromtimestamp(expires_millis / 1000, timezone.utc)
            else:
                if handle.get("token_created_time"):
                    created = datetime.fromtimestamp(float(handle["token_created_time"]) / 1000, timezone.utc)
                else:
                    created = datetime.fromisoformat(handle["token_created"].replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                # Android SessionHandle maps expire_in to expirationIntervalMinutes.
                expires = created + timedelta(minutes=float(handle["expire_in"]))
            return datetime.now(timezone.utc) + timedelta(seconds=30) >= expires
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return False

    def clear(self, *, persist_logout: bool = False) -> None:
        self.auth_token = None
        self.refresh_token = None
        self.diner_udid = None
        self.is_authenticated = False
        self.session_handle = None
        self.csrf_token = None
        self.login_generation = str(uuid4()) if persist_logout else None
        if persist_logout:
            # Credential-free marker cancels authentication responses begun before logout.
            self._save()
            return
        try:
            _SESSION_FILE.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove persisted session")


class GrubhubClient:
    """HTTP client with refresh-on-use and one authenticated 401 retry."""

    def __init__(self) -> None:
        self.session = GrubhubSession()
        self._refresh_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0, follow_redirects=True)

    def _headers(self, auth_required: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-gh-browser-id": self.session.browser_id,
            "Vary": "Accept-Encoding",
        }
        if self.session.auth_token and auth_required:
            headers["Authorization"] = f"Bearer {self.session.auth_token}"
        return headers

    @asynccontextmanager
    async def _disk_refresh_lock(self):
        """Serialize rotated refresh tokens across POSIX stdio processes."""
        if fcntl is None:
            yield
            return
        _SESSION_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(_SESSION_DIR / "session.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            yield
        finally:
            os.close(fd)

    @asynccontextmanager
    async def session_transition(self):
        """Coordinate login, logout and refresh commits, including across POSIX processes."""
        async with self._refresh_lock, self._disk_refresh_lock():
            if _SESSION_FILE.exists():
                self.session._load()
            else:
                self.session.clear()
            yield

    async def _refresh_token(
        self, expected_token: str | None = None,
        expected_identity: tuple[bool, str | None, str | None] | None = None,
    ) -> bool:
        identity = expected_identity or (self.session.is_authenticated, self.session.diner_udid, self.session.login_generation)
        async with self.session_transition():
            if not _SESSION_FILE.exists():
                return False
            current = (self.session.is_authenticated, self.session.diner_udid, self.session.login_generation)
            if current != identity:
                raise ValueError("Login changed in another invocation; retry with the current session")
            # Another invocation may have rotated this same login's tokens already.
            if expected_token is not None and self.session.auth_token != expected_token:
                return bool(self.session.auth_token)
            if not self.session.refresh_token:
                return False
            try:
                resp = await self._http.post(
                    "/auth/refresh",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    json={"brand": "GRUBHUB", "client_id": API_KEY,
                          "refresh_token": self.session.refresh_token},
                )
                if not resp.is_success:
                    logger.warning("Token refresh rejected (HTTP %s); login may be required", resp.status_code)
                    return False
                data = resp.json()
                handle = data.get("session_handle") or {}
                if not (handle.get("access_token") or data.get("auth_token")):
                    logger.warning("Token refresh response omitted an access token")
                    return False
                self.session.update_tokens(data)
                return True
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                logger.warning("Token refresh failed; retaining the existing session")
                return False

    async def _request(
        self, method: str, path: str, *, data: dict[str, Any] | None = None,
        params: Any = None, auth_required: bool = True,
    ) -> dict[str, Any]:
        identity = (self.session.is_authenticated, self.session.diner_udid, self.session.login_generation)
        refresh_attempted = False
        if auth_required and self.session.refresh_token and self.session.access_token_expiring():
            refresh_attempted = True
            await self._refresh_token(self.session.auth_token, identity)
        token = self.session.auth_token
        resp = await self._http.request(
            method, path, headers=self._headers(auth_required), json=data, params=params,
        )
        if resp.status_code == 401 and auth_required and self.session.refresh_token and not refresh_attempted:
            if await self._refresh_token(token, identity):
                resp = await self._http.request(
                    method, path, headers=self._headers(auth_required), json=data, params=params,
                )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def get(self, path: str, params: Any = None, auth_required: bool = True) -> dict[str, Any]:
        return await self._request("GET", path, params=params, auth_required=auth_required)

    async def post(
        self, path: str, data: dict[str, Any] | None = None,
        auth_required: bool = True, params: Any = None,
    ) -> dict[str, Any]:
        return await self._request("POST", path, data=data, params=params, auth_required=auth_required)

    async def put(
        self, path: str, data: dict[str, Any] | None = None, auth_required: bool = True,
    ) -> dict[str, Any]:
        return await self._request("PUT", path, data=data, auth_required=auth_required)

    async def delete(self, path: str, auth_required: bool = True) -> dict[str, Any]:
        return await self._request("DELETE", path, auth_required=auth_required)

    async def close(self) -> None:
        await self._http.aclose()


_client: GrubhubClient | None = None


def get_client() -> GrubhubClient:
    global _client
    if _client is None:
        _client = GrubhubClient()
    return _client
