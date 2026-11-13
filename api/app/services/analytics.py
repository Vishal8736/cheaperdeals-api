"""Profit and performance numbers. Cost uses each product's CURRENT supplier price (shipping and fees are not included)."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import CANCELLED, Connection, DELIVERED, FAILED_MANUAL, Order, TrackingEvent


def report(db: Session, cfg, days: int = 30) -> dict:
    since = utcnow() - timedelta(days=days)
    orders = db.query(Order).filter(Order.created_at >= since).order_by(Order.id).all()
    names = {c.id: c.name for c in db.query(Connection).all()}
    per_day: dict[str, dict] = {}
    for i in range(days):
        d = (utcnow() - timedelta(days=days - 1 - i)).replace(tzinfo=None)
        per_day[d.strftime("%d %b")] = {"revenue": Decimal("0"), "profit": Decimal("0"), "orders": 0}
    prod: dict[int, dict] = {}
    sup: dict[str, dict] = defaultdict(lambda: {"revenue": Decimal("0"), "profit": Decimal("0"), "orders": 0})
    statuses: dict[str, int] = defaultdict(int)
    rev = cost = Decimal("0")
    n = 0
    for o in orders:
        statuses[o.status] += 1
        if o.status == CANCELLED:
            continue
        n += 1
        o_cost = Decimal("0")
        sup_name = None
        for it in o.items:
            if it.product is not None:
                line_cost = Decimal(it.product.supplier_price) * it.quantity
                o_cost += line_cost
                row = prod.setdefault(it.product.id, {"title": it.product.title, "units": 0, "revenue": Decimal("0"), "profit": Decimal("0")})
                row["units"] += it.quantity
                line_rev = Decimal(it.unit_price) * it.quantity
                row["revenue"] += line_rev
                row["profit"] += line_rev - line_cost
                cid = it.product.supplier_connection_id
                sup_name = names.get(cid, "Removed connection") if cid else "Built-in supplier"
        o_rev = Decimal(o.total)
        rev += o_rev
        cost += o_cost
        key = o.created_at.strftime("%d %b")
        if key in per_day:
            per_day[key]["revenue"] += o_rev
            per_day[key]["profit"] += o_rev - o_cost
            per_day[key]["orders"] += 1
        s = sup[sup_name or "Unmatched products"]
        s["revenue"] += o_rev
        s["profit"] += o_rev - o_cost
        s["orders"] += 1
    ship_hours = []
    for ev in db.query(TrackingEvent).filter(TrackingEvent.status == "shipped", TrackingEvent.created_at >= since).all():
        if ev.order is not None:
            ship_hours.append((ev.created_at - ev.order.created_at).total_seconds() / 3600)
    profit = rev - cost
    done = statuses.get(DELIVERED, 0)
    # supplier scorecard: how reliable is each supplier (all-time for the period)
    card: dict[str, dict] = defaultdict(lambda: {"orders": 0, "delivered": 0, "failed": 0, "hours": []})
    for o in orders:
        if o.status == CANCELLED:
            continue
        cid = next((it.product.supplier_connection_id for it in o.items if it.product is not None), None)
        c = card[names.get(cid, "Removed connection") if cid else "Built-in supplier"]
        c["orders"] += 1
        c["delivered"] += int(o.status == DELIVERED)
        c["failed"] += int(o.status == FAILED_MANUAL)
        for ev in o.tracking:
            if ev.status == "shipped":
                c["hours"].append((ev.created_at - o.created_at).total_seconds() / 3600)
                break
    scorecard = [{"name": k, "orders": v["orders"], "delivered": v["delivered"], "failed": v["failed"],
                  "ok_pct": int((v["orders"] - v["failed"]) / v["orders"] * 100) if v["orders"] else 0,
                  "avg_ship_hours": round(sum(v["hours"]) / len(v["hours"]), 1) if v["hours"] else None} for k, v in card.items()]
    scorecard.sort(key=lambda r: (-r["orders"], r["name"]))
    return {
        "scorecard": scorecard, "dead": dead_stock(db),
        "days": days, "orders": n, "revenue": rev, "cost": cost, "profit": profit,
        "margin": (profit / rev * 100).quantize(Decimal("0.1")) if rev > 0 else Decimal("0"),
        "aov": (rev / n).quantize(Decimal("0.01")) if n else Decimal("0"),
        "per_day": [{"label": k, **v} for k, v in per_day.items()],
        "max_day": max([v["revenue"] for v in per_day.values()] + [Decimal("1")]),
        "top": sorted(prod.values(), key=lambda r: r["profit"], reverse=True)[:8],
        "suppliers": sorted(([k, v] for k, v in sup.items()), key=lambda kv: kv[1]["profit"], reverse=True),
        "statuses": sorted(statuses.items(), key=lambda kv: -kv[1]),
        "delivered": done, "failed": statuses.get(FAILED_MANUAL, 0),
        "success_pct": int(done / len(orders) * 100) if orders else 0,
        "avg_ship_hours": round(sum(ship_hours) / len(ship_hours), 1) if ship_hours else None,
    }


def dead_stock(db: Session, days: int = 60, limit: int = 15) -> list:
    """Active products with no order for `days` days (and older than that): candidates to delist."""
    from ..models import OrderItem, Product
    cutoff = utcnow() - timedelta(days=days)
    sold = {pid for (pid,) in db.query(OrderItem.product_id).join(Order, Order.id == OrderItem.order_id).filter(Order.created_at >= cutoff, OrderItem.product_id.isnot(None)).distinct()}
    out = []
    for p in db.query(Product).filter(Product.active.is_(True), Product.listing_status == "listed").order_by(Product.id):
        started = p.listed_at or p.updated_at
        if p.id not in sold and started is not None and started <= cutoff:
            out.append(p)
    return out[:limit]
