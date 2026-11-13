"""Money: the real profit-and-loss, cash waiting at suppliers, returns/refunds, and a printable invoice.
All amounts are USD (the store currency is assumed to be USD, as everywhere else in Cheape Deal).

  net profit = revenue - refunds - product cost + supplier refunds - shipping - payment fees
Costs use the supplier price frozen when the order arrived (not today's price). Shipping is our estimate for that country until you type the
real figure on the order. Payment fees are your gateway % from Finance settings."""
from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from ..models import Market, Order, ReturnCase
from . import settings_service as S
from .alerts import audit
from .outbound import emit

D = Decimal
ACTIVE_COST = ("submitted_to_supplier", "supplier_confirmed", "shipped")  # supplier has been paid, customer has not got it yet
TRANSITIONS = {"requested": {"approved", "rejected"}, "approved": {"received", "refunded", "rejected"}, "received": {"refunded"}, "rejected": set(), "refunded": set()}
BUSINESS_KEYS = ("business_name", "business_address", "business_gstin", "invoice_prefix", "payment_fee_pct")


def _q(v) -> Decimal:
    return D(v).quantize(D("0.01"), rounding=ROUND_HALF_UP)


# ---- business details ----------------------------------------------------------------------
def business(db: Session) -> dict:
    return {"business_name": S.get(db, "business_name", ""), "business_address": S.get(db, "business_address", ""), "business_gstin": S.get(db, "business_gstin", ""),
            "invoice_prefix": S.get(db, "invoice_prefix", "INV-") or "INV-", "payment_fee_pct": S.get(db, "payment_fee_pct", "3") or "3"}


def save_business(db: Session, form: dict) -> list[str]:
    try:
        fee = D(str(form.get("payment_fee_pct", "")).strip())
        if fee < 0 or fee > 50:
            raise ValueError
    except Exception:
        return ["Payment fee % must be a number from 0 to 50"]
    for k in ("business_name", "business_address", "business_gstin", "invoice_prefix"):
        S.set_value(db, k, str(form.get(k, "")).strip()[:300])
    S.set_value(db, "payment_fee_pct", str(fee))
    return []


# ---- shipping estimate ---------------------------------------------------------------------
def estimate_ship_cost(db: Session, order: Order) -> Decimal:
    m = db.query(Market).filter(Market.code == (order.ship_country_code or "").upper()).first()
    if m is None:
        return D("0")
    kg = sum((D(it.product.weight_kg or 0) * it.quantity for it in order.items if it.product is not None), D("0"))
    return _q(D(m.shipping_usd) + D(m.ship_per_kg_usd or 0) * kg)


def set_actual_ship_cost(db: Session, order: Order, value: str) -> None:
    try:
        v = D(str(value).strip())
        if v < 0 or v > D("100000"):
            raise ValueError
    except Exception:
        raise ValueError("Shipping cost must be a number (0 or more)")
    order.ship_cost_usd, order.ship_cost_actual = _q(v), True


# ---- profit and loss -----------------------------------------------------------------------
def order_figures(db: Session, o: Order, fee_pct: Decimal, refunds: dict) -> dict:
    cost = D("0")
    for it in o.items:
        unit = D(it.unit_cost) if D(it.unit_cost) > 0 else (D(it.product.supplier_price) if it.product is not None else D("0"))
        cost += unit * it.quantity
    rev = D(o.total)
    ref, sref = refunds.get(o.id, (D("0"), D("0")))
    fee = _q(rev * fee_pct / 100)
    ship = D(o.ship_cost_usd)
    net = rev - ref - cost + sref - ship - fee
    return {"order": o, "revenue": rev, "refund": ref, "cost": _q(cost), "supplier_refund": sref, "ship": ship, "fee": fee, "net": _q(net)}


def refunds_by_order(db: Session) -> dict:
    out: dict = defaultdict(lambda: (D("0"), D("0")))
    for r in db.query(ReturnCase).filter(ReturnCase.status == "refunded"):
        a, b = out[r.order_id]
        out[r.order_id] = (a + D(r.refund_usd), b + D(r.supplier_refund_usd))
    return out


def pnl(db: Session, months: int = 6) -> dict:
    fee_pct = D(business(db)["payment_fee_pct"])
    refunds = refunds_by_order(db)
    rows = [order_figures(db, o, fee_pct, refunds) for o in db.query(Order).filter(Order.status != "cancelled").order_by(Order.id)]
    per_month: dict[str, dict] = {}
    tot = {"revenue": D("0"), "refund": D("0"), "cost": D("0"), "supplier_refund": D("0"), "ship": D("0"), "fee": D("0"), "net": D("0"), "orders": 0}
    for r in rows:
        key = r["order"].created_at.strftime("%Y-%m")
        m = per_month.setdefault(key, {"revenue": D("0"), "refund": D("0"), "cost": D("0"), "supplier_refund": D("0"), "ship": D("0"), "fee": D("0"), "net": D("0"), "orders": 0})
        for t in (m, tot):
            for k in ("revenue", "refund", "cost", "supplier_refund", "ship", "fee", "net"):
                t[k] += r[k]
            t["orders"] += 1
    tied = sum((r["cost"] for r in rows if r["order"].status in ACTIVE_COST), D("0"))
    estimated = sum(1 for r in rows if not r["order"].ship_cost_actual and r["ship"] > 0)
    return {"rows": rows[-100:][::-1], "months": sorted(per_month.items(), reverse=True)[:months], "total": tot, "tied_up": _q(tied),
            "margin": (tot["net"] / tot["revenue"] * 100).quantize(D("0.1")) if tot["revenue"] > 0 else D("0"), "ship_estimated": estimated,
            "open_returns": db.query(ReturnCase).filter(ReturnCase.status.in_(["requested", "approved", "received"])).count()}


# ---- returns -------------------------------------------------------------------------------
def open_return(db: Session, order: Order, reason: str, actor: str = "admin") -> ReturnCase:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Write the reason for the return")
    if db.query(ReturnCase).filter(ReturnCase.order_id == order.id, ReturnCase.status.in_(["requested", "approved", "received"])).count():
        raise ValueError("This order already has an open return")
    r = ReturnCase(order_id=order.id, reason=reason[:300], status="requested")
    db.add(r)
    db.flush()
    audit(db, "order", order.id, "return_requested", detail=reason[:300], actor=actor)
    return r


def move_return(db: Session, rt, r: ReturnCase, to: str, *, refund: str = "", supplier_refund: str = "", note: str = "", actor: str = "admin") -> None:
    if to not in TRANSITIONS.get(r.status, set()):
        raise ValueError(f"A '{r.status}' return cannot become '{to}'")
    if to == "refunded":
        try:
            ref = D(str(refund).strip() or "0")
            sref = D(str(supplier_refund).strip() or "0")
            if ref < 0 or sref < 0 or ref > D(r.order.total) * 2:
                raise ValueError
        except Exception:
            raise ValueError("Refund amounts must be numbers (0 or more, and not more than the order total)")
        if ref <= 0:
            raise ValueError("Enter how much you refunded the customer")
        r.refund_usd, r.supplier_refund_usd = _q(ref), _q(sref)
    prev = r.status
    r.status = to
    if note.strip():
        r.note = (r.note + "\n" if r.note else "") + note.strip()[:500]
    audit(db, "order", r.order_id, f"return_{to}", from_state=prev, to_state=to, detail=(f"refund ${r.refund_usd}" if to == "refunded" else note[:200]), actor=actor)
    emit(db, f"return.{to}", {"order_id": r.order_id, "number": r.order.store_order_number, "refund_usd": str(r.refund_usd)})
    if to == "refunded":
        try:  # tell the store in the order notes (internal). The money itself you refund in your store / payment provider.
            rt.store_for(r.order.store_connection_id).add_order_note(r.order.store_order_id, f"Cheape Deal: refund of ${r.refund_usd} recorded.", customer_note=False)
        except Exception:
            pass


# ---- invoice -------------------------------------------------------------------------------
def invoice(db: Session, order: Order) -> dict:
    b = business(db)
    lines = [{"title": it.title or it.sku, "qty": it.quantity, "unit": D(it.unit_price), "total": _q(D(it.unit_price) * it.quantity)} for it in order.items]
    return {"business": b, "number": f"{b['invoice_prefix']}{order.store_order_number or order.id}", "date": order.created_at, "lines": lines,
            "total": D(order.total), "currency": order.currency or "USD"}
