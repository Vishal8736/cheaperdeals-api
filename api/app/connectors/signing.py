"""Request signing used by the Alibaba-group open platforms. Pure functions, unit-tested.

  iop  - Alibaba-style "IOP" gateway:  HMAC-SHA256(secret, sorted name+value pairs), upper-case hex
         (sign_method=md5 falls back to the older style: md5(secret + pairs + secret))
  aop  - 1688 gateway:  HMAC-SHA1(secret, urlPath + sorted name+value pairs), upper-case hex, sent as _aop_signature
  top  - Taobao TOP gateway:  md5(secret + sorted name+value pairs + secret), upper-case hex
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any


def _s(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def pairs(params: dict) -> str:
    return "".join(f"{k}{_s(params[k])}" for k in sorted(params) if params[k] is not None and params[k] != "")


def sign_iop(secret: str, params: dict, method: str = "sha256", api_path: str = "") -> str:
    """api_path: only for the system gateway (/rest/auth/token/create...): the path is put in front of the pairs. Business calls (/sync): none."""
    base = api_path + pairs({k: v for k, v in params.items() if k != "sign"})
    if method == "md5":
        return hashlib.md5((secret + base + secret).encode(), usedforsecurity=False).hexdigest().upper()
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest().upper()


def sign_aop(secret: str, url_path: str, params: dict) -> str:
    return hmac.new(secret.encode(), (url_path + pairs(params)).encode(), hashlib.sha1).hexdigest().upper()


def sign_top(secret: str, params: dict) -> str:
    return hashlib.md5((secret + pairs(params) + secret).encode(), usedforsecurity=False).hexdigest().upper()


def iop_params(app_key: str, api: str, params: dict, access_token: str = "", sign_method: str = "sha256", now_ms: int | None = None) -> dict:
    p = {"app_key": app_key, "method": api, "sign_method": sign_method, "timestamp": str(now_ms or int(time.time() * 1000))}
    if access_token:
        p["session"] = access_token  # business calls take the token as `session` (system calls use access_token)
    p.update({k: _s(v) for k, v in params.items() if v is not None})
    return p


def top_timestamp() -> str:  # TOP wants China time
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
