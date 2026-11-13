"""Order pipeline: intake -> validate -> (approve) -> supplier -> retries. All business rules live here so they are testable
without n8n. n8n only calls the /internal/jobs/* endpoints on a schedule."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..adapters.base import (
    NormalizedOrder, Notifier, StoreAdapter, StoreError, SupplierAdapter, SupplierError, SupplierLine,
    SupplierOrderRequest,
)
from ..config import Config
from ..db import utcnow
from ..models import (
    ALLOWED_TRANSITIONS, ProductListing, Approval, CANCELLED, CONFIRMED, DELIVERED, FAILED_MANUAL, FAILED_RETRY, IllegalTransition,
    Order, OrderItem, PENDING_APPROVAL, Product, RECEIVED, SHIPPED, SUBMITTED, SupplierOrder, VALIDATED,
)
from . import settings_service as S
from .alerts import audit, raise_alert
from .agents import event as agent_event
from .outbound import emit

LOCK_TTL = timedelta(minutes=5)


@dataclass
class Runtime:
    cfg: Config
    supplier: SupplierAdapter
    store: StoreAdapter
    notifier: Notifier
    connectors: object | None = None  # ConnectorManager; None in simple setups/tests

    def supplier_for(self, connection_id: int | None = None):
        """Adapter for a product/order's supplier connection; falls back to the built-in (or default) supplier."""
        return self.connectors.supplier(connection_id, self.supplier) if self.connectors is not None else self.supplier

    def store_for(self, connection_id: int | None = None):
        """Adapter for the store an order came from. None = the built-in store."""
        return self.connectors.store(connection_id, self.store) if (self.connectors is not None and connection_id) else self.store

    def alert(self, db: Session, **kw):
        return raise_alert(db, self.notifier, public_url=self.cfg.public_url, **kw)


# ---- state machine -------------------------------------------------------------------------
def transition(db: Session, order: Order, to: str, *, action: str, detail: str = "", actor: str = "system") -> None:
    if to not in ALLOWED_TRANSITIONS.get(order.status, set()):
        raise IllegalTransition(f"{order.status} -> {to} is not allowed")
    frm = order.status
    order.status = to
    order.updated_at = utcnow()
    audit(db, "order", order.id, action, from_state=frm, to_state=to, detail=detail, actor=actor)
    db.flush()
    _announce(db, order, to, detail)


_ANNOUNCE = {
    VALIDATED: ("router", "ok", "passed the checks"),
    PENDING_APPROVAL: ("router", "warn", "is waiting for your approval"),
    SUBMITTED: ("router", "ok", "was placed with the supplier"),
    CONFIRMED: ("tracking", "ok", "was confirmed by the supplier"),
    SHIPPED: ("tracking", "ok", "has shipped"),
    DELIVERED: ("tracking", "ok", "was delivered"),
    FAILED_RETRY: ("router", "warn", "hit a temporary problem and will be retried"),
    FAILED_MANUAL: ("router", "error", "needs you"),
    CANCELLED: ("router", "info", "was cancelled"),
}


_OUT = {SUBMITTED: "order.placed", SHIPPED: "order.shipped", DELIVERED: "order.delivered", FAILED_MANUAL: "order.needs_attention",
        PENDING_APPROVAL: "order.needs_approval", CANCELLED: "order.cancelled"}


def _announce(db: Session, order: Order, to: str, detail: str) -> None:
    if to in _OUT:
        emit(db, _OUT[to], {"order_id": order.id, "number": order.store_order_number, "status": to, "total": str(order.total), "currency": order.currency,
                            "detail": detail[:200]})
    a = _ANNOUNCE.get(to)
    if a:
        agent_event(db, a[0], f"Order #{order.store_order_number} {a[2]}" + (f" ({detail[:140]})" if detail and to in {FAILED_RETRY, FAILED_MANUAL} else ""),
                    a[1], order.id)


# ---- intake (idempotent) -------------------------------------------------------------------
def _match_product(db: Session, store_product_id: str, sku: str) -> Product | None:
    p = db.query(Product).filter(Product.store_product_id == store_product_id).first() if store_product_id else None
    if p is None and store_product_id:  # products we published on a custom website are known by the id it returned
        lst = db.query(ProductListing).filter(ProductListing.remote_id == store_product_id).first()
        p = lst.product if lst else None
    if p is None and sku:
        p = db.query(Product).filter(Product.sku == sku).first()
    return p


def intake(db: Session, norm: NormalizedOrder, source: str = "webhook", store_connection_id: int | None = None) -> tuple[Order | None, bool, str]:
    """Returns (order, created, reason). Same store_order_id twice -> one row (created=False)."""
    if not norm.paid:
        return None, False, f"ignored: store status '{norm.status}' is not paid"
    existing = db.query(Order).filter(Order.store_order_id == norm.store_order_id).first()
    if existing:
        return existing, False, "duplicate"
    o = Order(
        store_order_id=norm.store_order_id, store_order_number=norm.number, customer_name=norm.customer_name,
        customer_email=norm.email, customer_phone=norm.phone, ship_address1=norm.address1, ship_address2=norm.address2,
        ship_city=norm.city, ship_state=norm.state, ship_zip=norm.zip_code, ship_country_code=norm.country_code,
        ship_country=norm.country, total=norm.total, currency=norm.currency, source=source, status=RECEIVED, store_connection_id=store_connection_id,
    )
    for it in norm.items:
        prod = _match_product(db, it.store_product_id, it.sku)
        o.items.append(OrderItem(product_id=prod.id if prod else None, store_product_id=it.store_product_id, sku=it.sku,
                                 title=it.title, quantity=it.quantity, unit_price=it.unit_price, unit_cost=Decimal(prod.supplier_price) if prod else Decimal("0")))
    try:
        with db.begin_nested():  # savepoint: a duplicate insert must not poison the outer transaction
            db.add(o)
            db.flush()
    except IntegrityError:
        existing = db.query(Order).filter(Order.store_order_id == norm.store_order_id).first()
        return existing, False, "duplicate"
    from . import finance as FIN
    o.ship_cost_usd = FIN.estimate_ship_cost(db, o)
    audit(db, "order", o.id, "received", to_state=RECEIVED, detail=f"source={source}")
    emit(db, "order.received", {"order_id": o.id, "number": o.store_order_number, "total": str(o.total), "currency": o.currency})
    agent_event(db, "intake", f"New order #{o.store_order_number} from {o.customer_name or 'a customer'}: "
                              f"{sum(i.quantity for i in o.items)} item(s), total {o.total} {o.currency}".strip(), "info", o.id)
    return o, True, "created"


# ---- locking -------------------------------------------------------------------------------
def claim(db: Session, order_id: int) -> str | None:
    """Atomic claim so webhook-triggered processing and the sweeper never submit the same order twice."""
    token = uuid.uuid4().hex
    now = utcnow()
    res = db.execute(
        update(Order).where(Order.id == order_id,
                            or_(Order.lock_token.is_(None), Order.locked_at < now - LOCK_TTL))
        .values(lock_token=token, locked_at=now))
    db.commit()
    return token if res.rowcount == 1 else None


def release(db: Session, order_id: int, token: str) -> None:
    db.execute(update(Order).where(Order.id == order_id, Order.lock_token == token).values(lock_token=None, locked_at=None))
    db.commit()


# ---- validation ----------------------------------------------------------------------------
def effective_min_margin(db: Session, p: Product) -> Decimal:
    return Decimal(p.min_margin_pct) if p.min_margin_pct is not None else S.get_decimal(db, "min_margin_pct")


def validate(db: Session, order: Order) -> list[str]:
    errs: list[str] = []
    live = not S.get_bool(db, "dry_run")
    if not order.items:
        errs.append("Order has no items")
    for it in order.items:
        p = it.product
        label = it.title or it.sku or it.store_product_id
        if p is None:
            errs.append(f"'{label}' is not in Products yet - add it with its supplier variant ID")
            continue
        if not p.active:
            errs.append(f"'{p.title}' is switched off in Products")
        if not p.supplier_vid:
            errs.append(f"'{p.title}' has no supplier variant ID")
        if p.stock < it.quantity:
            errs.append(f"'{p.title}' is out of stock at the supplier (have {p.stock}, need {it.quantity})")
        unit = Decimal(it.unit_price)
        if unit <= 0:
            errs.append(f"'{p.title}' has a zero price in the order")
        else:
            margin = ((unit - Decimal(p.supplier_price)) / unit * 100).quantize(Decimal("0.01"))
            floor = effective_min_margin(db, p)
            if margin < floor:
                errs.append(f"'{p.title}' margin {margin}% is below the {floor}% minimum "
                            f"(paid {unit}, supplier cost {p.supplier_price})")
    if live:
        if not S.get(db, "default_logistic_name").strip():
            errs.append("Set the shipping method name in Settings before going live")
        if not order.ship_country_code:
            errs.append("Order has no shipping country")
    return errs


# ---- processing ----------------------------------------------------------------------------
def _fail_manual(db: Session, rt: Runtime, order: Order, msg: str, *, actor: str = "system") -> dict:
    order.last_error = msg[:2000]
    order.next_retry_at = None
    transition(db, order, FAILED_MANUAL, action="failed_manual", detail=msg, actor=actor)
    rt.alert(db, level="error", source="order", message=f"Order #{order.store_order_number} needs you: {msg}",
             order_id=order.id, dedupe_key=f"fm-{order.id}")
    db.commit()
    return {"outcome": "failed_manual", "error": msg}


def _schedule_retry(db: Session, rt: Runtime, order: Order, msg: str) -> dict:
    order.retry_count += 1
    max_r = S.get_int(db, "max_retries")
    if order.retry_count > max_r:
        return _fail_manual(db, rt, order, f"Retries exhausted ({max_r}). Last error: {msg}")
    base = max(1, S.get_int(db, "retry_base_minutes"))
    delay = min(base * 2 ** (order.retry_count - 1), 360)
    order.next_retry_at = utcnow() + timedelta(minutes=delay)
    order.last_error = msg[:2000]
    transition(db, order, FAILED_RETRY, action="retry_scheduled", detail=f"attempt {order.retry_count}/{max_r} in {delay}m: {msg}")
    db.commit()
    return {"outcome": "retry_scheduled", "in_minutes": delay, "error": msg}


def _local_day_start_utc(cfg: Config) -> datetime:
    tz = cfg.tzinfo
    now_local = datetime.now(timezone.utc).astimezone(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None)


def _record_created(db: Session, rt: Runtime, order: Order, so: SupplierOrder, supplier_order_id: str, status: str,
                    raw: dict, *, dry: bool) -> dict:
    so.supplier_order_id = supplier_order_id
    so.state = "dry_run" if dry else "created"
    so.supplier_status = status
    so.response_json = json.dumps(raw, default=str)[:20000]
    order.last_error = ""
    order.next_retry_at = None
    if order.status != SUBMITTED:
        transition(db, order, SUBMITTED, action="dry_run_submitted" if dry else "submitted",
                   detail=f"supplier order {supplier_order_id}")
    db.commit()
    if not dry:
        try:
            rt.store_for(order.store_connection_id).add_order_note(order.store_order_id, f"Cheape Deal: placed with supplier (ref {supplier_order_id}).",
                                    customer_note=False)
        except StoreError as e:
            rt.alert(db, level="warning", source="store", message=f"Could not add note to store order {order.store_order_id}: {e}",
                     order_id=order.id, dedupe_key=f"note-{order.id}")
            db.commit()
    return {"outcome": "dry_run_submitted" if dry else "submitted", "supplier_order_id": supplier_order_id}


def _resolve_ambiguous(db: Session, rt: Runtime, order: Order, so: SupplierOrder, why: str) -> dict:
    """The supplier call may or may not have created the order. Never blindly retry."""
    try:
        sup = rt.supplier_for(order_supplier_connection(order))
    except SupplierError as e:
        return _fail_manual(db, rt, order, f"{e}. Check the supplier dashboard for {so.order_number}.")
    if sup.supports_lookup:
        try:
            found = sup.find_order(so.order_number)
        except SupplierError as e:
            return _fail_manual(db, rt, order, f"Unsure if supplier created the order ({why}); lookup failed ({e}). "
                                               f"Check the supplier dashboard for {so.order_number}.")
        if found:
            return _record_created(db, rt, order, so, found.supplier_order_id, found.status, found.raw, dry=False)
        order.supplier_order = None  # confirmed absent -> drop the stale attempt so the retry starts clean
        db.flush()
        return _schedule_retry(db, rt, order, f"{why} (supplier confirmed no order exists)")
    return _fail_manual(db, rt, order, f"Supplier call may have gone through ({why}). Check the supplier dashboard for "
                                       f"order number {so.order_number}, then answer the question on this order's page.")


def _build_request(db: Session, order: Order, order_number: str) -> SupplierOrderRequest:
    return SupplierOrderRequest(
        order_number=order_number, customer_name=order.customer_name, email=order.customer_email, phone=order.customer_phone,
        address1=order.ship_address1, address2=order.ship_address2, city=order.ship_city, province=order.ship_state,
        zip_code=order.ship_zip, country_code=order.ship_country_code, country=order.ship_country,
        logistic_name=S.get(db, "default_logistic_name").strip(), from_country_code=S.get(db, "from_country_code").strip(),
        lines=[SupplierLine(vid=i.product.supplier_vid, quantity=i.quantity, sku=i.sku) for i in order.items],
        remark=f"Store order #{order.store_order_number}",
    )


def order_supplier_connection(order: Order) -> int | None:
    """Which supplier connection serves this order. Mixed suppliers raise (we never guess how to split)."""
    ids = {i.product.supplier_connection_id for i in order.items if i.product is not None}
    if len(ids) > 1:
        raise SupplierError("This order contains products from different suppliers. Split it or place the parts by hand.")
    return next(iter(ids)) if ids else None


def _submit(db: Session, rt: Runtime, order: Order) -> dict:
    dry = S.get_bool(db, "dry_run")
    order_number = f"CD-{order.store_order_id}"
    so = order.supplier_order
    if so and so.supplier_order_id:  # already placed -> make sure state reflects it, never place twice
        if order.status not in {SUBMITTED, CONFIRMED, SHIPPED, DELIVERED}:
            transition(db, order, SUBMITTED, action="submitted", detail=f"already placed: {so.supplier_order_id}")
            db.commit()
        return {"outcome": "already_submitted", "supplier_order_id": so.supplier_order_id}

    # Daily cap (counted per mode so rehearsals do not eat the live cap)
    cap = S.get_int(db, "daily_order_cap")
    if cap > 0:
        used = (db.query(SupplierOrder).filter(SupplierOrder.created_at >= _local_day_start_utc(rt.cfg),
                                               SupplierOrder.dry_run.is_(dry), SupplierOrder.state != "attempting").count())
        if used >= cap:
            rt.alert(db, level="warning", source="daily_cap", dedupe_key=f"cap-{utcnow().date()}",
                     message=f"Daily order cap ({cap}) reached. Remaining orders wait until tomorrow or raise the cap in Settings.")
            db.commit()
            return {"outcome": "daily_cap"}

    if dry:
        if so is None:
            so = SupplierOrder(order_number=order_number, dry_run=True)
            order.supplier_order = so  # attach via the relationship so in-memory state stays correct
        so.request_json = json.dumps({"dry_run": True, "lines": [(i.product.supplier_vid, i.quantity) for i in order.items]})
        return _record_created(db, rt, order, so, f"DRY-{order_number}", "dry_run", {"dry_run": True}, dry=True)

    if so is None:
        so = SupplierOrder(order_number=order_number, dry_run=False, state="attempting")
        order.supplier_order = so
    elif so.state == "attempting" and so.updated_at is not None:
        # A previous attempt left no result: it crashed or timed out. Resolve that first.
        db.commit()
        return _resolve_ambiguous(db, rt, order, so, "previous attempt has no recorded result")
    try:
        sup = rt.supplier_for(order_supplier_connection(order))
    except SupplierError as e:
        order.supplier_order = None
        db.flush()
        return _fail_manual(db, rt, order, str(e))
    req = _build_request(db, order, order_number)
    so.request_json = json.dumps(req, default=lambda o: o.__dict__)[:20000]
    so.state = "attempting"
    db.commit()  # persist the attempt BEFORE the network call
    try:
        res = sup.create_order(req)
    except SupplierError as e:
        if e.ambiguous:
            return _resolve_ambiguous(db, rt, order, so, str(e))
        order.supplier_order = None  # definitely not created -> clear the attempt
        db.flush()
        if e.retryable:
            return _schedule_retry(db, rt, order, str(e))
        return _fail_manual(db, rt, order, str(e))
    return _record_created(db, rt, order, so, res.supplier_order_id, res.status, res.raw, dry=False)


def process_order(db: Session, rt: Runtime, order_id: int, *, actor: str = "system") -> dict:
    token = claim(db, order_id)
    if not token:
        return {"outcome": "locked"}
    try:
        order = db.get(Order, order_id)
        db.refresh(order)
        return _process_locked(db, rt, order, actor)
    except IllegalTransition as e:
        db.rollback()
        return {"outcome": "illegal_transition", "error": str(e)}
    finally:
        release(db, order_id, token)


def _process_locked(db: Session, rt: Runtime, order: Order, actor: str) -> dict:
    if S.get_bool(db, "paused"):
        return {"outcome": "paused"}
    st = order.status
    if st in {SUBMITTED, CONFIRMED, SHIPPED, DELIVERED, CANCELLED, FAILED_MANUAL}:
        return {"outcome": "noop", "status": st}
    if st == FAILED_RETRY and order.next_retry_at and order.next_retry_at > utcnow():
        return {"outcome": "not_due"}
    if st == PENDING_APPROVAL:
        return {"outcome": "awaiting_approval"}

    errs = validate(db, order)
    if errs:
        return _fail_manual(db, rt, order, "; ".join(errs), actor=actor)

    threshold = S.get_decimal(db, "approval_threshold")
    approved = any(a.status == "approved" for a in order.approvals)
    from . import risk as RISK
    risky, risk_reason = RISK.check(db, order)
    over = threshold > 0 and Decimal(order.total) >= threshold
    if (over or risky) and not approved:
        reason = " | ".join(x for x in ([f"Order total {order.total} is at or above the approval threshold {threshold}"] if over else []) + ([risk_reason] if risky else []))
        order.approvals.append(Approval(status="pending", reason=reason))
        if order.status in {RECEIVED, FAILED_RETRY}:
            transition(db, order, VALIDATED, action="validated")
        transition(db, order, PENDING_APPROVAL, action="approval_requested", detail=reason)
        rt.alert(db, level="warning", source="approval", message=f"Order #{order.store_order_number} waits for your approval. {reason}",
                 order_id=order.id, dedupe_key=f"appr-{order.id}")
        db.commit()
        return {"outcome": "needs_approval"}

    if order.status in {RECEIVED, FAILED_RETRY}:
        transition(db, order, VALIDATED, action="validated")
        db.commit()
    return _submit(db, rt, order)


def process_pending(db: Session, rt: Runtime, limit: int = 50) -> dict:
    if S.get_bool(db, "paused"):
        return {"paused": True, "processed": 0}
    now = utcnow()
    ids = [r[0] for r in db.query(Order.id).filter(
        or_(Order.status.in_([RECEIVED, VALIDATED]), (Order.status == FAILED_RETRY) & (Order.next_retry_at <= now))
    ).order_by(Order.id).limit(limit).all()]
    summary: dict[str, int] = {}
    for oid in ids:
        res = process_order(db, rt, oid)
        summary[res["outcome"]] = summary.get(res["outcome"], 0) + 1
    S.mark_run(db, "process_pending", json.dumps(summary))
    db.commit()
    return {"paused": False, "processed": len(ids), "outcomes": summary}


# ---- operator actions ----------------------------------------------------------------------
def approve(db: Session, rt: Runtime, order_id: int, actor: str = "admin") -> dict:
    order = db.get(Order, order_id)
    pend = next((a for a in order.approvals if a.status == "pending"), None)
    if order.status != PENDING_APPROVAL or not pend:
        return {"outcome": "not_pending"}
    pend.status, pend.decided_at = "approved", utcnow()
    transition(db, order, VALIDATED, action="approved", actor=actor)
    db.commit()
    return process_order(db, rt, order_id, actor=actor)


def reject(db: Session, rt: Runtime, order_id: int, actor: str = "admin") -> dict:
    order = db.get(Order, order_id)
    pend = next((a for a in order.approvals if a.status == "pending"), None)
    if order.status != PENDING_APPROVAL or not pend:
        return {"outcome": "not_pending"}
    pend.status, pend.decided_at = "rejected", utcnow()
    transition(db, order, CANCELLED, action="rejected", detail="Rejected in approvals. Refund the customer in your store if needed.", actor=actor)
    db.commit()
    return {"outcome": "cancelled"}


def retry(db: Session, rt: Runtime, order_id: int, actor: str = "admin") -> dict:
    order = db.get(Order, order_id)
    if order.status not in {FAILED_MANUAL, FAILED_RETRY}:
        return {"outcome": "not_retryable", "status": order.status}
    if order.supplier_order and order.supplier_order.state == "attempting":
        return {"outcome": "needs_resolution"}  # operator must say whether the supplier got it
    order.retry_count, order.next_retry_at = 0, None
    transition(db, order, VALIDATED, action="operator_retry", actor=actor)
    db.commit()
    return process_order(db, rt, order_id, actor=actor)


def cancel(db: Session, rt: Runtime, order_id: int, actor: str = "admin") -> dict:
    order = db.get(Order, order_id)
    if order.status in {CANCELLED, DELIVERED, SHIPPED}:
        return {"outcome": "not_cancellable", "status": order.status}
    placed = bool(order.supplier_order and order.supplier_order.supplier_order_id and not order.supplier_order.dry_run)
    transition(db, order, CANCELLED, action="cancelled", actor=actor,
               detail="Supplier order was already placed - cancel it in the supplier dashboard too." if placed else "")
    db.commit()
    return {"outcome": "cancelled", "supplier_order_placed": placed}


def resolve_ambiguous(db: Session, rt: Runtime, order_id: int, *, supplier_order_id: str | None, actor: str = "admin") -> dict:
    """Operator checked the supplier dashboard. supplier_order_id given = 'it was placed'; None = 'it was not placed'."""
    order = db.get(Order, order_id)
    so = order.supplier_order
    if not so or so.state != "attempting":
        return {"outcome": "nothing_to_resolve"}
    if supplier_order_id:
        if order.status == FAILED_MANUAL:
            transition(db, order, VALIDATED, action="operator_resolved_placed", actor=actor)
        return _record_created(db, rt, order, so, supplier_order_id.strip(), "operator_confirmed", {"by": actor}, dry=False)
    order.supplier_order = None
    order.retry_count, order.next_retry_at = 0, None
    if order.status in {FAILED_MANUAL, FAILED_RETRY}:
        transition(db, order, VALIDATED, action="operator_resolved_not_placed", actor=actor)
    db.commit()
    return process_order(db, rt, order_id, actor=actor)


def redo_live(db: Session, rt: Runtime, order_id: int, actor: str = "admin") -> dict:
    """An order that arrived during DRY-RUN was only rehearsed. Place it for real once live mode is on."""
    order = db.get(Order, order_id)
    so = order.supplier_order
    if S.get_bool(db, "dry_run"):
        return {"outcome": "still_dry_run"}
    if order.status != SUBMITTED or not so or not so.dry_run:
        return {"outcome": "not_a_rehearsal"}
    order.supplier_order = None
    order.retry_count, order.next_retry_at = 0, None
    transition(db, order, VALIDATED, action="redo_live", detail="Rehearsal discarded; placing for real", actor=actor)
    db.commit()
    return process_order(db, rt, order_id, actor=actor)


def ingest_payload(db: Session, rt: Runtime, payload: dict, source: str, store_connection_id: int | None = None) -> tuple[Order | None, bool, str]:
    norm = rt.store_for(store_connection_id).normalize_order(payload)
    order, created, reason = intake(db, norm, source, store_connection_id)
    db.commit()
    return order, created, reason
