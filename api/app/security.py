from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from collections import defaultdict, deque

import bcrypt
from fastapi import HTTPException, Request


class NotAuthenticated(Exception):
    pass


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt(rounds=12)).decode()


def check_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    except ValueError:
        return False


class LoginLimiter:
    """5 failed logins per 10 minutes per IP (in-memory; resets on restart - fine for a single-admin tool)."""

    def __init__(self, max_attempts: int = 5, window: int = 600):
        self.max, self.window = max_attempts, window
        self.fails: dict[str, deque] = defaultdict(deque)

    def _prune(self, ip: str) -> deque:
        q, now = self.fails[ip], time.monotonic()
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, ip: str) -> bool:
        return len(self._prune(ip)) >= self.max

    def fail(self, ip: str) -> None:
        self._prune(ip).append(time.monotonic())

    def reset(self, ip: str) -> None:
        self.fails.pop(ip, None)


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


async def verify_csrf(request: Request) -> None:
    expected = request.session.get("csrf", "")
    sent = request.headers.get("x-csrf-token", "")
    if not sent:
        form = await request.form()
        sent = str(form.get("csrf_token", ""))
    if not expected or not hmac.compare_digest(expected.encode(), sent.encode()):  # bytes: a non-ASCII header must be a 403, not a crash
        raise HTTPException(status_code=403, detail="CSRF check failed. Reload the page and try again.")


# ---- 2FA (TOTP, RFC 6238: the 6-digit code in Google Authenticator / Microsoft Authenticator / Authy) -------------
def totp_new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, at: float | None = None) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((time.time() if at is None else at) // 30)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


def totp_ok(secret: str, code: str, window: int = 1) -> bool:
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6 or not secret:
        return False
    now = time.time()
    return any(hmac.compare_digest(totp_code(secret, now + 30 * d), code) for d in range(-window, window + 1))


def totp_uri(secret: str, account: str, issuer: str = "Cheape Deal") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"


# ---- roles: admin (everything) | operator (daily work, no settings/connections/system/users) | viewer (look only) ----
ADMIN_ONLY = ("/settings", "/connect", "/system", "/users", "/email", "/keys", "/platforms", "/website/connect", "/diagnostics", "/assistant/settings", "/assistant/test", "/assistant/models", "/assistant/review")
VIEWER_POST_OK = ("/logout", "/assistant/ask")


def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise NotAuthenticated()
    role = request.session.get("role", "admin")
    if role != "admin":
        path = request.url.path
        if any(path == a or path.startswith(a + "/") for a in ADMIN_ONLY):
            raise HTTPException(status_code=403, detail="Only the admin can open this page.")
        if role == "viewer" and request.method not in ("GET", "HEAD") and path not in VIEWER_POST_OK:
            raise HTTPException(status_code=403, detail="Your account is view-only.")
    return user


def require_internal_key(request: Request) -> None:
    key = request.app.state.cfg.internal_api_key
    sent = request.headers.get("x-internal-key", "")
    if not key or not hmac.compare_digest(key.encode(), sent.encode()):
        raise HTTPException(status_code=401, detail="Bad internal key")
