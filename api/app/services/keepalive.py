"""While the server is awake it calls its own public /ping every 10 minutes. On Render's free plan that stops the 15-minute idle sleep.

This alone cannot WAKE a sleeping server (nothing is running then), so also set up one outside pinger: the GitHub Actions
workflow in .github/workflows/keepalive.yml or a free UptimeRobot monitor. Both are described in docs/WEBSITE_DEPLOY.md.
"""
from __future__ import annotations

import logging
import threading

import time

import httpx

log = logging.getLogger("cheapedeal.keepalive")
INTERVAL = 600
LAST: dict = {}  # the newest self-ping result, shown on the API Keys page


def ping(url: str, path: str = "/ping", timeout: float = 15.0) -> dict:
    """One keep-alive ping. Returns {ok, status, ms, url, error}. This is the whole trick: any request keeps a free Render service awake for another ~15 minutes."""
    full = url.strip().rstrip("/") + path
    t0 = time.monotonic()
    try:
        r = httpx.get(full, timeout=timeout, follow_redirects=True)
        res = {"ok": r.status_code == 200, "status": r.status_code, "ms": int((time.monotonic() - t0) * 1000), "url": full, "error": ""}
    except Exception as e:
        res = {"ok": False, "status": 0, "ms": int((time.monotonic() - t0) * 1000), "url": full, "error": type(e).__name__}
    res["at"] = time.strftime("%H:%M:%S")
    LAST.clear()
    LAST.update(res)
    return res


def start(public_url: str) -> threading.Event | None:
    """Returns a stop-event, or None when no public https address is known (local runs)."""
    url = (public_url or "").strip().rstrip("/")
    if not url.startswith("https://"):
        return None
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(INTERVAL):
            try:
                ping(url, timeout=10.0)
            except Exception as e:  # never let the helper die or make noise
                log.debug("self-ping failed: %s", e)

    threading.Thread(target=loop, name="keepalive", daemon=True).start()
    return stop
