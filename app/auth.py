"""Password login using a signed session cookie.

Basic auth is avoided because home-screen web apps on iOS cannot show the
browser's sign-in box. The cookie value is an HMAC derived from SITE_PASSWORD,
so changing the password signs everyone out.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time

COOKIE_NAME = "dl_session"
COOKIE_MAX_AGE = 30 * 24 * 3600  # stay signed in for 30 days
MAX_FAILURES = 10                # failed logins allowed per client ...
FAILURE_WINDOW = 15 * 60         # ... within this many seconds

# Reachable without signing in: the login screen, its stylesheet, and what the browser needs
# to install the app (manifest, icons, service worker).
PUBLIC_PATHS = {"/login", "/login.html", "/api/login", "/app.css", "/manifest.webmanifest", "/sw.js", "/offline.html"}
PUBLIC_PREFIXES = ("/icons/",)


def site_password() -> str:
    return os.environ.get("SITE_PASSWORD", "")


def session_token(password: str) -> str:
    return hmac.new(password.encode(), b"downloader-session-v1", hashlib.sha256).hexdigest()


def is_signed_in(cookie: str | None, password: str) -> bool:
    return bool(cookie) and secrets.compare_digest(cookie, session_token(password))


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def safe_next(target: str | None) -> str:
    """Only allow redirects to paths on this site (blocks //evil.com and https://...)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


class LoginLimiter:
    """In-memory count of failed logins per client, to slow down password guessing."""

    def __init__(self, max_failures: int = MAX_FAILURES, window: float = FAILURE_WINDOW):
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, client: str, now: float) -> list[float]:
        times = [t for t in self._failures.get(client, []) if now - t < self.window]
        self._failures[client] = times
        return times

    def retry_after(self, client: str, now: float | None = None) -> int:
        """Seconds the client must wait, or 0 if it may try again."""
        now = time.time() if now is None else now
        with self._lock:
            times = self._recent(client, now)
            if len(times) < self.max_failures:
                return 0
            return max(1, int(self.window - (now - times[0])))

    def record_failure(self, client: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._recent(client, now).append(now)

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


def client_id(headers, fallback: str) -> str:
    # Render's proxy appends the real client IP as the last X-Forwarded-For entry;
    # earlier entries can be forged by the client, so they are ignored.
    forwarded = headers.get("x-forwarded-for", "")
    return forwarded.split(",")[-1].strip() or fallback
