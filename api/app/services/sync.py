"""Scheduled jobs called by n8n: tracking sync, stock + price sync, missed-order sweep, nightly reconciliation."""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from ..adapters.base import StoreError, SupplierError
from ..db import utcnow
from ..models import (
    CANCELLED, CONFIRMED, DELIVERED, FAILED_MANUAL, FAILED_RETRY, Order, Product, PriceHistory, RECEIVED, SHIPPED,
    SUBMITTED, SupplierOrder, TrackingEvent, VALIDATED,
)
from . import fx
from . import settings_service as S
from .agents import event as agent_event
from .outbound import emit
from .pricing import sell_price
from . import playbook as PB
from .orders import Runtime, effective_min_margin, ingest_payload, order_supplier_connection, transition

STATUS_MAP = {"confirmed": CONFIRMED, "shipped": SHIPPED, "delivered": DELIVERED, "cancelled": CANCELLED}
RANK = {SUBMITTED: 0, CONFIRMED: 1, SHIPPED: 2, DELIVERED: 3}


# ---- tracking ------------------------------------------------------------------------------
def _customer_message(status: str, tracking: str, carrier: str) -> str | None:
    if status == SHIPPED:
        bits = ["Good news - your order has shipped."]
        if carrier:
            bits.append(f"Carrier: {carrier}.")
        if tracking:
            bits.append(f"Tracking number: {tracking}.")
        return " ".join(bits)
    if status == DELIVERED:
        return "Your order has been delivered. Thank you for shopping with us."
    return None


def _notify_store(db: Session, rt: Runtime, order: Order, ev: TrackingEvent) -> None:
    """Send exactly one customer message per shipped/delivered event; retried on later runs if the store was down."""
    msg = _customer_message(ev.status, ev.tracking_number, ev.carrier)
    if msg is None:
        ev.customer_notified = True
        return
    try:
        store = rt.store_for(order.store_connection_id)
        store.add_order_note(order.store_order_id, msg, customer_note=True)
        if ev.status == DELIVERED:
            store.set_order_status(order.store_order_id, "completed")
        ev.customer_notified = True
    except StoreError as e:
        rt.alert(db, level="warning", source="store", message=f"Could not update store for order #{order.store_order_number}: {e}",
                 order_id=order.id, dedupe_key=f"notify-{order.id}-{ev.status}")
    _email_customer(db, rt, order, ev, msg)


def _email_customer(db: Session, rt: Runtime, order: Order, ev: TrackingEvent, msg: str) -> None:
    """Optional direct email (setting 'customer_emails'). Sent once per event; a failure is retried on the next run."""
    from . import mailer
    if ev.emailed or not mailer.customer_enabled(db) or not order.customer_email:
        return
    base = (rt.cfg.public_url or "").rstrip("/")
    link = f"\n\nTrack your order: {base}/track/{order.public_token}" if base and order.public_token else ""
    biz = S.get(db, "business_name", "") or "Our store"
    ok, why = mailer.send(db, order.customer_email, f"Your order {order.store_order_number or order.id}: update", f"Hello {order.customer_name},\n\n{msg}{link}\n\nThank you,\n{biz}\n", biz)
    if ok:
        ev.emailed = True
        stats_note = "customer emailed"
        agent_event(db, "tracking", f"Order #{order.store_order_number}: {stats_note}", "ok")
    else:
        rt.alert(db, level="info", source="email", dedupe_key=f"em-{order.id}-{ev.status}",
                 message=f"Could not email the customer for order #{order.store_order_number}: {why}")


def tracking_sync(db: Session, rt: Runtime) -> dict:
    if S.get_bool(db, "paused"):
        return {"paused": True}
    stats = {"checked": 0, "changed": 0, "errors": 0, "notified": 0}
    orders = (db.query(Order).join(SupplierOrder, SupplierOrder.order_id == Order.id)
              .filter(Order.status.in_([SUBMITTED, CONFIRMED, SHIPPED]), SupplierOrder.dry_run.is_(False),
                      SupplierOrder.supplier_order_id.isnot(None)).order_by(Order.id).all())
    for order in orders:
        so = order.supplier_order
        stats["checked"] += 1
        try:
            info = rt.supplier_for(order_supplier_connection(order)).get_order_info(so.supplier_order_id)
        except SupplierError as e:
            stats["errors"] += 1
            rt.alert(db, level="warning", source="tracking", message=f"Tracking check failed for order #{order.store_order_number}: {e}",
                     order_id=order.id, dedupe_key=f"trk-{order.id}")
            continue
        so.supplier_status = info.raw_status or info.status
        new_status = STATUS_MAP.get(info.status)
        last = order.tracking[-1] if order.tracking else None
        tracking_changed = bool(info.tracking_number) and (last is None or last.tracking_number != info.tracking_number)
        status_changed = False
        if new_status == CANCELLED:
            transition(db, order, CANCELLED, action="supplier_cancelled", detail=f"supplier status {info.raw_status}")
            rt.alert(db, level="error", source="supplier", order_id=order.id, dedupe_key=f"sc-{order.id}",
                     message=f"Supplier cancelled order #{order.store_order_number}. Refund or reorder.")
            status_changed = True
        elif new_status and RANK.get(new_status, -1) > RANK.get(order.status, -1):
            transition(db, order, new_status, action="supplier_status", detail=f"supplier status {info.raw_status}")
            status_changed = True
        if status_changed or tracking_changed:
            ev = TrackingEvent(status=order.status, tracking_number=info.tracking_number,
                               carrier=info.carrier, detail=info.raw_status)
            order.tracking.append(ev)
            db.flush()
            stats["changed"] += 1
            if order.status in {SHIPPED, DELIVERED}:
                _notify_store(db, rt, order, ev)
                stats["notified"] += int(ev.customer_notified)
            else:
                ev.customer_notified = True
        db.commit()
    # catch up on events the store missed earlier (store was down)
    stale = (db.query(TrackingEvent).filter(TrackingEvent.customer_notified.is_(False)).all())
    for ev in stale:
        _notify_store(db, rt, ev.order, ev)
        stats["notified"] += int(ev.customer_notified)
    S.mark_run(db, "tracking_sync", json.dumps(stats))
    db.commit()
    return stats


# ---- stock + price -------------------------------------------------------------------------
def _store_targets(p: Product) -> list[tuple[int | None, str]]:
    """Every (store connection id, remote product id) this product is live on. None = the built-in store."""
    out: list[tuple[int | None, str]] = []
    if p.listing_status == "listed" and p.store_product_id and not p.store_product_id.startswith("draft-"):
        out.append((None, p.store_product_id))
    out += [(l.connection_id, l.remote_id) for l in p.listings if l.connection_id and l.status == "listed" and l.remote_id]
    return out


def _push_price(rt: Runtime, p: Product, price: Decimal) -> None:
    """New price for every store that follows the product price. Stores with their own market price (multi-country) are left alone."""
    own = {l.connection_id for l in p.listings if l.price_usd is not None}
    for cid, rid in _store_targets(p):
        if cid in own:
            continue
        rt.store_for(cid).set_product_price(rid, price)


def _push_visible(rt: Runtime, p: Product, visible: bool) -> None:
    for cid, rid in _store_targets(p):
        rt.store_for(cid).set_product_visible(rid, visible)


def _prefetch(rt: Runtime, products: list[Product], workers: int = 6) -> dict:
    """Ask the suppliers about every product at the same time (network only; the database is touched afterwards, one product at a
    time). A slow supplier no longer holds up the others. Never raises: a failure is stored as that product's result."""
    from concurrent.futures import ThreadPoolExecutor

    def one(item):
        pid, adapter, vid = item
        try:
            return pid, adapter.get_product_info(vid)
        except SupplierError as e:
            return pid, e
        except Exception as e:  # a buggy adapter must not kill the sync
            return pid, SupplierError(f"unexpected supplier error: {e}", retryable=True)
    work = [(p.id, rt.supplier_for(p.supplier_connection_id), p.supplier_vid) for p in products]  # adapters resolved here, on this thread
    if len(work) < 3:
        return dict(one(w) for w in work)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(one, work))


def stock_price_sync(db: Session, rt: Runtime) -> dict:
    if S.get_bool(db, "paused"):
        return {"paused": True}
    stats = {"checked": 0, "repriced": 0, "hidden": 0, "unhidden": 0, "errors": 0, "failovers": 0}
    markup = S.get_decimal(db, "markup_pct")
    ending = S.get(db, "price_ending")
    products = db.query(Product).filter(Product.supplier_vid != "", Product.source != "sourcing", Product.kind != "affiliate").order_by(Product.id).all()
    fetched = _prefetch(rt, products)
    for p in products:
        stats["checked"] += 1
        err = None
        got = fetched.get(p.id)
        if isinstance(got, SupplierError):
            info, err = None, got
        else:
            info = got
        if (info is None or info.stock <= 0) and p.backup_vid:  # failover: the backup supplier takes over if it has stock
            try:
                binfo = rt.supplier_for(p.backup_connection_id).get_product_info(p.backup_vid)
                if binfo.stock > 0:
                    p.supplier_connection_id, p.backup_connection_id = p.backup_connection_id, p.supplier_connection_id
                    p.supplier_vid, p.backup_vid = p.backup_vid, p.supplier_vid
                    info, err = binfo, None
                    stats["failovers"] += 1
                    agent_event(db, "stock", f"'{p.title}' switched to its backup supplier (the main one had no stock)", "warn")
                    emit(db, "product.failover", {"product_id": p.id, "title": p.title})
                    rt.alert(db, level="info", source="failover", dedupe_key=f"fo-{p.id}-{utcnow().date()}",
                             message=f"'{p.title}' now ships from its backup supplier because the main one was out of stock or unreachable.")
            except SupplierError:
                pass
        if info is None:
            stats["errors"] += 1
            if isinstance(err, SupplierError) and err.retryable:  # CJ busy / slow down: the next run retries, nobody needs to be alerted for it
                stats["retry_later"] = stats.get("retry_later", 0) + 1
            else:
                rt.alert(db, level="warning", source="stock_sync", message=f"Could not read supplier data for '{p.title}': {err}",
                         dedupe_key=f"ss-{p.id}")
            continue
        new_cost = info.price
        if (p.cost_currency or "USD").upper() != "USD":  # the supplier quotes in another currency (e.g. INR): keep our books in USD
            try:
                new_cost = fx.to_usd(db, info.price, p.cost_currency)
            except fx.FxError as e:
                stats["errors"] += 1
                rt.alert(db, level="warning", source="stock_sync", message=f"Cannot convert the supplier price of '{p.title}': {e}", dedupe_key=f"fxc-{p.id}")
                continue
        old_cost = Decimal(p.supplier_price)
        p.stock = info.stock
        p.supplier_price = new_cost
        p.last_synced_at = utcnow()

        # 1) reprice (store first, local second: a store failure is retried next run)
        if new_cost != old_cost and not p.manual_override and not p.strategy_managed and markup >= 0:
            new_sell = sell_price(new_cost, PB.markup_for(db, new_cost, markup), ending)
            if new_sell != Decimal(p.selling_price):
                try:
                    _push_price(rt, p, new_sell)
                    p.selling_price = new_sell
                    stats["repriced"] += 1
                except StoreError as e:
                    rt.alert(db, level="warning", source="store", dedupe_key=f"px-{p.id}",
                             message=f"Could not push new price for '{p.title}': {e}")
        if new_cost != old_cost:
            db.add(PriceHistory(product_id=p.id, supplier_price=p.supplier_price, selling_price=p.selling_price))

        # 2) visibility guard: never sell at a loss, never sell what the supplier does not have
        floor = effective_min_margin(db, p)
        reason = ""
        if p.stock <= 0:
            reason = "Out of stock at the supplier"
        elif p.margin_pct() < floor:
            reason = f"Margin {p.margin_pct()}% is below the {floor}% minimum"
        try:
            if reason and not p.auto_hidden:
                _push_visible(rt, p, False)
                p.auto_hidden, p.hidden_reason = True, reason
                stats["hidden"] += 1
                rt.alert(db, level="warning", source="stock_sync", dedupe_key=f"hide-{p.id}",
                         message=f"'{p.title}' hidden automatically: {reason}.")
            elif reason and p.auto_hidden and p.hidden_reason != reason:
                p.hidden_reason = reason
            elif not reason and p.auto_hidden:
                if p.active:
                    _push_visible(rt, p, True)
                p.auto_hidden, p.hidden_reason = False, ""
                stats["unhidden"] += 1
        except StoreError as e:
            rt.alert(db, level="warning", source="store", dedupe_key=f"vis-{p.id}",
                     message=f"Could not change visibility of '{p.title}' in the store: {e}")
        db.commit()
    S.mark_run(db, "stock_price_sync", json.dumps(stats))
    if stats.get("retry_later"):
        agent_event(db, "stock", f"{stats['retry_later']} product ka supplier data abhi nahi mila (supplier busy): agli baar dobara dekhunga.", "warn")
    db.commit()
    return stats


# ---- missed orders + reconciliation --------------------------------------------------------
def sweep_missed_orders(db: Session, rt: Runtime, hours: int = 48, *, ingest: bool = True) -> dict:
    """Compare the store's recent paid orders with our DB (catches dropped webhooks)."""
    try:
        rows = rt.store.list_recent_paid_orders(hours)
    except StoreError as e:
        rt.alert(db, level="warning", source="sweep", message=f"Could not read recent store orders: {e}", dedupe_key="sweep-read")
        db.commit()
        return {"error": str(e), "missing": 0}
    missing, ingested = [], 0
    for payload in rows:
        norm = rt.store.normalize_order(payload)
        if not norm.paid or db.query(Order.id).filter(Order.store_order_id == norm.store_order_id).first():
            continue
        missing.append(norm.store_order_id)
        if ingest:
            order, created, _ = ingest_payload(db, rt, payload, "sweep")
            if created:
                ingested += 1
                rt.alert(db, level="warning", source="sweep", order_id=order.id, dedupe_key=f"sweep-{norm.store_order_id}",
                         message=f"Store order {norm.store_order_id} never arrived by webhook. Imported by the sweep. "
                                 f"Check the webhook in your store.")
    return {"store_orders": len(rows), "missing": len(missing), "ingested": ingested, "missing_ids": missing}


def reconcile(db: Session, rt: Runtime, hours: int = 48) -> dict:
    report: dict = {"problems": []}
    sweep = sweep_missed_orders(db, rt, hours, ingest=False)
    report["store_check"] = sweep
    for oid in sweep.get("missing_ids", []):
        report["problems"].append(f"Store order {oid} is paid but missing in Cheape Deal")
        rt.alert(db, level="error", source="reconcile", dedupe_key=f"rec-miss-{oid}",
                 message=f"Store order {oid} is paid in the store but missing in Cheape Deal.")
    now = utcnow()
    stuck = db.query(Order).filter(Order.status.in_([RECEIVED, VALIDATED]), Order.created_at < now - timedelta(minutes=30)).all()
    for o in stuck:
        msg = f"Order #{o.store_order_number} has been waiting {int((now - o.created_at).total_seconds() // 60)} min without reaching the supplier"
        report["problems"].append(msg)
        rt.alert(db, level="warning", source="reconcile", order_id=o.id, dedupe_key=f"rec-stuck-{o.id}", message=msg)
    attempting = db.query(SupplierOrder).filter(SupplierOrder.state == "attempting",
                                                SupplierOrder.updated_at < now - timedelta(minutes=10)).all()
    for so in attempting:
        msg = f"Order #{so.order.store_order_number}: supplier result unknown for {so.order_number}. Check the supplier dashboard."
        report["problems"].append(msg)
        rt.alert(db, level="error", source="reconcile", order_id=so.order_id, dedupe_key=f"rec-att-{so.id}", message=msg)
    broken = (db.query(Order).outerjoin(SupplierOrder, SupplierOrder.order_id == Order.id)
              .filter(Order.status.in_([SUBMITTED, CONFIRMED, SHIPPED, DELIVERED]), SupplierOrder.id.is_(None)).all())
    for o in broken:
        msg = f"Order #{o.store_order_number} is '{o.status}' but has no supplier order record"
        report["problems"].append(msg)
        rt.alert(db, level="error", source="reconcile", order_id=o.id, dedupe_key=f"rec-broken-{o.id}", message=msg)
    report["needs_attention"] = db.query(Order).filter(Order.status.in_([FAILED_MANUAL, FAILED_RETRY])).count()
    S.mark_run(db, "reconcile", f"{len(report['problems'])} problems")
    db.commit()
    return report


# ---- progress the supplier reported outside any API (seller by email, phone, WhatsApp) -------
def manual_progress(db: Session, rt: Runtime, order_id: int, stage: str, tracking: str = "", carrier: str = "", actor: str = "admin") -> dict:
    """You tell Cheape Deal what the seller told you. Same effect as the tracking sync: status moves forward, the customer is told once."""
    order = db.get(Order, order_id)
    if order is None:
        return {"ok": False, "error": "Order not found"}
    target = {"confirmed": CONFIRMED, "shipped": SHIPPED, "delivered": DELIVERED}.get(stage)
    if target is None:
        return {"ok": False, "error": "Choose confirmed, shipped or delivered"}
    so = order.supplier_order
    if so is None or so.dry_run or order.status not in (SUBMITTED, CONFIRMED, SHIPPED):
        return {"ok": False, "error": "This order is not waiting for the seller (it is a rehearsal, or not placed yet)."}
    tracking, carrier = (tracking or "").strip()[:128], (carrier or "").strip()[:128]
    if target == SHIPPED and not tracking:
        return {"ok": False, "error": "Enter the tracking number the seller gave you."}
    if RANK.get(target, -1) < RANK.get(order.status, -1) or (target == order.status and not (target == SHIPPED and tracking)):
        return {"ok": False, "error": f"The order is already '{order.status}'."}
    if target != order.status:
        transition(db, order, target, action="seller_update", detail=f"entered by {actor}" + (f", tracking {tracking}" if tracking else ""), actor=actor)
    so.supplier_status = stage
    ev = TrackingEvent(status=order.status, tracking_number=tracking, carrier=carrier, detail=f"entered by {actor}")
    order.tracking.append(ev)
    db.flush()
    if order.status in {SHIPPED, DELIVERED}:
        _notify_store(db, rt, order, ev)
    else:
        ev.customer_notified = True
    agent_event(db, "tracking", f"Order #{order.store_order_number}: seller update entered ({stage})", "ok")
    emit(db, f"order.{stage}", {"order_id": order.id, "number": order.store_order_number, "tracking": tracking, "carrier": carrier})
    return {"ok": True, "status": order.status, "told_customer": ev.customer_notified}
