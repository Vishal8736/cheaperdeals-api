"""Shop files + small checks used by the launcher. Vercel/Render/GitHub/Neon are driven through their own CLIs (see hostcli.py); no API keys."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import shopcfg as C

SITE_DIR = Path(__file__).resolve().parent.parent / "storefront"
SHIP = ["index.html", "app.js", "tailwind.css", "config.js", "legal.js", "favicon.svg", "privacy.html", "terms.html", "refund.html", "shipping.html"]


class HostError(Exception):
    """Kept for callers that catch it; the CLI layer raises hostcli.CliError."""


def site_files(backend_url: str) -> list[dict]:
    """The shop, ready to upload: every file as text, plus a vercel.json that forwards /api to the backend."""
    out = []
    for name in SHIP:
        out.append({"file": name, "data": (SITE_DIR / name).read_text(encoding="utf-8")})
    cfg = {"rewrites": [{"source": "/api/:path*", "destination": backend_url.rstrip("/") + "/:path*"}],
           "headers": [{"source": "/(.*)", "headers": [{"key": "X-Content-Type-Options", "value": "nosniff"}, {"key": "Referrer-Policy", "value": "strict-origin-when-cross-origin"},
                                                        {"key": "X-Frame-Options", "value": "DENY"}]}]}
    out.append({"file": "vercel.json", "data": json.dumps(cfg, indent=2)})
    return out


def valid_project_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,50}[a-z0-9]", name or ""))


def alive(url: str, path: str = "/ping") -> bool:
    """True when the address answers /ping with 'ok' (works through the Vercel /api rewrite too)."""
    try:
        r = C.client().get(url.rstrip("/") + path, timeout=15)
        return r.status_code == 200 and "ok" in r.text.lower()[:40]
    except Exception:
        return False
