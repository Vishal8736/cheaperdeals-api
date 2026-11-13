"""Outgoing webhooks: tell YOUR systems (Zapier, n8n, your site, a Discord bot...) what Cheape Deal just did.

Events are held until the database commit succeeds, then POSTed in a background thread so a slow receiver never slows an order.
Header X-CheapeDeal-Signature = HMAC-SHA256 hex of the exact body, key = the secret from Settings.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from . import settings_service as S

log = logging.getLogger("cheapedeal.outbound")
SENDER = None  # tests replace this: SENDER(url, secret, body_bytes)


def _post(url: str, secret: str, body: bytes) -> None:
    headers = {"Content-Type": "application/json", "User-Agent": "CheapeDeal/2"}
    if secret:
        headers["X-CheapeDeal-Signature"] = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    for attempt in range(3):
        try:
            if httpx.post(url, content=body, headers=headers, timeout=8.0).status_code < 400:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2 * (attempt + 1))
    log.warning("outgoing webhook to %s failed after 3 tries", url)


def _dispatch(rows: list[tuple[str, str, bytes]]) -> None:
    for url, secret, body in rows:
        if SENDER is not None:
            SENDER(url, secret, body)
        else:
            threading.Thread(target=_post, args=(url, secret, body), daemon=True).start()


def emit(db: Session, name: str, data: dict) -> None:
    try:
        url = S.get(db, "outgoing_webhook_url", "").strip()
        if not url:
            return
        body = json.dumps({"event": name, "time": datetime.now(timezone.utc).isoformat(), "data": data}, default=str).encode()
        row = (url, S.get(db, "outgoing_webhook_secret", ""), body)
        if db.in_transaction():
            db.info.setdefault("pending_out", []).append(row)
        else:
            _dispatch([row])
    except Exception:
        log.exception("could not queue outgoing webhook")


@sa_event.listens_for(Session, "after_commit")
def _send_after_commit(session: Session) -> None:
    rows = session.info.pop("pending_out", None)
    if rows:
        _dispatch(rows)


@sa_event.listens_for(Session, "after_rollback")
def _drop_after_rollback(session: Session) -> None:
    session.info.pop("pending_out", None)
