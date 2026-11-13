from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ..services import orders as O

router = APIRouter()
log = logging.getLogger("cheapedeal.webhook")


def _process_in_background(app, order_id: int) -> None:
    db = app.state.database.SessionLocal()
    try:
        O.process_order(db, app.state.rt, order_id)
    except Exception:  # never lose the order: the sweeper will pick it up
        log.exception("background processing failed for order %s", order_id)
    finally:
        db.close()


@router.post("/webhooks/woocommerce")
async def woocommerce_webhook(request: Request, background: BackgroundTasks):
    """WooCommerce -> Cheape Deal. Signature is checked on the raw bytes, then the order is saved BEFORE anything else."""
    raw = await request.body()
    app = request.app
    sig = request.headers.get("x-wc-webhook-signature", "")
    if not app.state.rt.store.verify_webhook(raw, sig):
        raise HTTPException(status_code=401, detail="Bad webhook signature")
    try:
        payload = json.loads(raw)
    except ValueError:
        return {"ok": True, "ignored": "not JSON (probably the WooCommerce ping)"}
    if not isinstance(payload, dict) or "id" not in payload:
        return {"ok": True, "ignored": "no order id"}
    db = app.state.database.SessionLocal()
    try:
        order, created, reason = O.ingest_payload(db, app.state.rt, payload, "webhook")
        order_id = order.id if order else None
    finally:
        db.close()
    if order_id and created:
        background.add_task(_process_in_background, app, order_id)
    return {"ok": True, "order_id": order_id, "created": created, "reason": reason}


@router.post("/webhooks/store/{connection_id}")
async def custom_store_webhook(connection_id: int, request: Request, background: BackgroundTasks):
    """Your own website -> Cheape Deal. Body = JSON order (see docs/CONNECTORS.md). Header X-Signature = HMAC-SHA256 hex of the raw body."""
    raw = await request.body()
    app = request.app
    try:
        adapter = app.state.rt.store_for(connection_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Unknown or disabled store connection")
    sig = request.headers.get("x-signature") or request.headers.get("x-hub-signature-256") or request.headers.get("x-shopify-hmac-sha256") or ""
    if not adapter.verify_webhook(raw, sig):
        raise HTTPException(status_code=401, detail="Bad webhook signature")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not JSON")
    if not isinstance(payload, dict) or "id" not in payload:
        raise HTTPException(status_code=400, detail="Order JSON needs an id")
    db = app.state.database.SessionLocal()
    try:
        order, created, reason = O.ingest_payload(db, app.state.rt, payload, "webhook", store_connection_id=connection_id)
        order_id = order.id if order else None
    finally:
        db.close()
    if order_id and created:
        background.add_task(_process_in_background, app, order_id)
    return {"ok": True, "order_id": order_id, "created": created, "reason": reason}
