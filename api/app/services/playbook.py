"""Playbook: the dropshipping strategies that run by themselves every hour (all explained on the Playbook page).

1. Tiered markup      - cheap items get a bigger markup %, expensive ones a smaller one (used when a product is imported / its cost changes)
2. Demand pricing     - a product that sells fast gets a small price rise (up to a cap); the rise is removed when demand cools down
3. Clearance          - a product with stock that has not sold for N days gets a price cut, never below your minimum margin
4. Sale calendar      - dated campaigns (Diwali, Black Friday...) give a temporary discount, never below your minimum margin
5. Cheapest supplier  - if the backup supplier is clearly cheaper (and has stock) it becomes the main supplier
6. Auto-retire        - products with no sale for a long time are switched off (you can switch them back on)

Every price change keeps the margin floor, skips products you fixed by hand (manual override) or that the Strategy agent prices,
and is pushed to the stores first (a store failure is retried next hour). Nothing here talks to a customer.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..adapters.base import StoreError
from ..db import utcnow
from ..models import Order, OrderItem, Product
from . import settings_service as S
from .agents import event as agent_event
from .pricing import sell_price

log = logging.getLogger("cheapedeal.playbook")
D = Decimal

DEFAULTS = {
    "pb_tier_on": "false", "pb_tiers": "0-20:80, 20-100:50, 100-500:35, 500+:25",
    "pb_demand_on": "true", "pb_demand_days": "7", "pb_demand_orders": "3", "pb_demand_step": "5", "pb_demand_cap": "25",
    "pb_clear_on": "true", "pb_clear_days": "30", "pb_clear_step": "15",
    "pb_sale_on": "true", "pb_sales": "",
    "pb_cheapest_on": "true", "pb_cheapest_pct": "5",
    "pb_retire_on": "false", "pb_retire_days": "90",
}
BOOLS = ("pb_tier_on", "pb_demand_on", "pb_clear_on", "pb_sale_on", "pb_cheapest_on", "pb_retire_on")


def cfg(db: Session) -> dict:
    return {k: S.get(db, k, v) for k, v in DEFAULTS.items()}


def save(db: Session, form: dict) -> list[str]:
    """Validate and store the Playbook form. Returns a list of problems (nothing is saved when there are any)."""
    errs: list[str] = []
    clean: dict[str, str] = {k: ("true" if form.get(k) in ("on", "true", "1") else "false") for k in BOOLS}
    for k, lo, hi, label in (("pb_demand_days", 1, 90, "Demand: din"), ("pb_demand_orders", 1, 1000, "Demand: orders"), ("pb_demand_step", 1, 50, "Demand: badhao %"),
                             ("pb_demand_cap", 1, 100, "Demand: max badhao %"), ("pb_clear_days", 7, 365, "Clearance: din"), ("pb_clear_step", 1, 80, "Clearance: ghatao %"),
                             ("pb_cheapest_pct", 1, 90, "Sasta supplier: kam se kam fark %"), ("pb_retire_days", 14, 730, "Retire: din")):
        try:
            v = int(str(form.get(k, DEFAULTS[k])).strip())
            if not lo <= v <= hi:
                raise ValueError
            clean[k] = str(v)
        except ValueError:
            errs.append(f"{label} {lo} se {hi} ke beech ek poora number hona chahiye.")
    tiers = str(form.get("pb_tiers", "")).strip()
    if parse_tiers(tiers) is None:
        errs.append("Tier format galat hai. Aise likho: 0-20:80, 20-100:50, 100+:30  (cost range : markup %).")
    clean["pb_tiers"] = tiers
    sales = str(form.get("pb_sales", "")).strip()
    bad = [ln for ln in sales.splitlines() if ln.strip() and parse_sale(ln) is None]
    if bad:
        errs.append("Sale line galat hai: '" + bad[0][:60] + "'. Aise likho: Diwali|2026-11-08|2026-11-14|20")
    clean["pb_sales"] = sales
    if not errs:
        for k, v in clean.items():
            S.set_value(db, k, v)
    return errs


# ---- 1. tiered markup ----------------------------------------------------------------------
def parse_tiers(text: str):
    """'0-20:80, 20-100:50, 100+:30' -> [(low, high|None, pct)] or None when the text is not valid."""
    out = []
    try:
        for part in [x.strip() for x in text.split(",") if x.strip()]:
            rng, pct = part.split(":")
            pct = D(pct.strip())
            if pct < 0 or pct > 1000:
                return None
            rng = rng.strip()
            if rng.endswith("+"):
                out.append((D(rng[:-1]), None, pct))
            else:
                lo, hi = rng.split("-")
                if D(hi) <= D(lo):
                    return None
                out.append((D(lo), D(hi), pct))
    except Exception:
        return None
    return out or None


def markup_for(db: Session, cost: Decimal, default: Decimal) -> Decimal:
    """The markup % for a product costing `cost`: the matching tier when tiered markup is on, otherwise the global markup."""
    if S.get(db, "pb_tier_on", "false") != "true":
        return default
    tiers = parse_tiers(S.get(db, "pb_tiers", DEFAULTS["pb_tiers"])) or []
    for lo, hi, pct in tiers:
        if D(cost) >= lo and (hi is None or D(cost) < hi):
            return pct
    return default


# ---- 4. sale calendar ----------------------------------------------------------------------
def parse_sale(line: str):
    try:
        name, a, b, pct = [x.strip() for x in line.split("|")]
        s, e, pct = date.fromisoformat(a), date.fromisoformat(b), D(pct)
        if e < s or not (0 < pct < 90) or not name:
            return None
        return name, s, e, pct
    except Exception:
        return None


def active_sale(db: Session, today: date | None = None):
    today = today or utcnow().date()
    best = None
    for ln in S.get(db, "pb_sales", "").splitlines():
        sale = parse_sale(ln) if ln.strip() else None
        if sale and sale[1] <= today <= sale[2] and (best is None or sale[3] > best[3]):
            best = sale
    return best


# ---- the run -------------------------------------------------------------------------------
def _sold(db: Session, product_id: int, days: int) -> int:
    since = utcnow() - timedelta(days=days)
    q = (db.query(func.coalesce(func.sum(OrderItem.quantity), 0)).join(Order, Order.id == OrderItem.order_id)
         .filter(OrderItem.product_id == product_id, Order.created_at >= since, Order.status.notin_(("cancelled", "failed", "rejected"))))
    return int(q.scalar() or 0)


def _floor(db: Session, p: Product) -> Decimal:
    from .orders import effective_min_margin
    m = effective_min_margin(db, p)
    cost = D(p.supplier_price)
    return (cost / (1 - m / 100)).quantize(D("0.01"), rounding=ROUND_CEILING) if 0 <= m < 100 and cost > 0 else D("0")


def _round(price: D, ending: str) -> D:
    return sell_price(price, D("0"), ending)


def _decide(db: Session, p: Product, c: dict, base: D, now: datetime, sale) -> tuple[D, str]:
    """The price this product should have right now, and why ('' = normal price)."""
    pct, why = D("0"), []
    age_from = p.listed_at or p.playbook_since or now
    if c["pb_demand_on"] == "true" and _sold(db, p.id, int(c["pb_demand_days"])) >= int(c["pb_demand_orders"]) and p.stock > 0:
        sold = _sold(db, p.id, int(c["pb_demand_days"]))
        steps = sold // int(c["pb_demand_orders"])
        pct = min(D(c["pb_demand_step"]) * steps, D(c["pb_demand_cap"]))
        why.append(f"demand +{pct}%")
    elif c["pb_clear_on"] == "true" and p.stock > 0 and (now - age_from).days >= int(c["pb_clear_days"]) and _sold(db, p.id, int(c["pb_clear_days"])) == 0:
        pct = -D(c["pb_clear_step"])
        why.append(f"clearance {pct}%")
    if sale is not None and c["pb_sale_on"] == "true":
        pct -= sale[3]
        why.append(f"sale {sale[0]} -{sale[3]}%")
    if pct == 0:
        return base, ""
    price = _round((base * (1 + pct / 100)).quantize(D("0.01")), S.get(db, "price_ending"))
    floor = _floor(db, p)
    if price < floor:  # never below the margin floor; if the normal price is already lower than the floor, never go below that
        price = max(floor, min(base, floor)) if base >= floor else base
        why.append("floor")
    return price, ", ".join(why)


def run(db: Session, rt, now: datetime | None = None) -> dict:
    from . import sync as Y
    stats = {"off": False, "repriced": 0, "restored": 0, "switched": 0, "retired": 0, "changes": 0, "errors": 0}
    if S.get_bool(db, "paused") or not S.get_bool(db, "playbook_enabled"):
        stats["off"] = True
        return stats
    c, now = cfg(db), now or utcnow()
    sale = active_sale(db, now.date())
    for p in db.query(Product).filter(Product.active.is_(True), Product.listing_status == "listed", Product.kind != "affiliate").all():
        try:
            if p.playbook_since is None:
                p.playbook_since = now
            if p.manual_override or p.strategy_managed or D(p.supplier_price) <= 0:
                continue
            # the base price: what the price would be without the Playbook. If someone/sync changed the price since we set it, that is the new base.
            if p.playbook_tag and p.playbook_price is not None and D(p.selling_price) == D(p.playbook_price) and p.playbook_base is not None:
                base = D(p.playbook_base)
            else:
                base = D(p.selling_price)
            target, why = _decide(db, p, c, base, now, sale)
            if why and abs(target - D(p.selling_price)) >= D("0.01"):
                Y._push_price(rt, p, target)
                p.playbook_base, p.playbook_price, p.playbook_tag = base, target, why
                p.selling_price = target
                stats["repriced"] += 1
            elif not why and p.playbook_tag:
                if D(p.selling_price) == D(p.playbook_price or 0) and base != D(p.selling_price):
                    Y._push_price(rt, p, base)
                    p.selling_price = base
                    stats["restored"] += 1
                p.playbook_base = p.playbook_price = None
                p.playbook_tag = ""
            # 5. cheapest supplier
            if c["pb_cheapest_on"] == "true" and p.backup_vid:
                if _switch_if_cheaper(db, rt, p, D(c["pb_cheapest_pct"])):
                    stats["switched"] += 1
            # 6. auto-retire
            if c["pb_retire_on"] == "true" and (now - (p.listed_at or p.playbook_since or now)).days >= int(c["pb_retire_days"]) \
                    and _sold(db, p.id, int(c["pb_retire_days"])) == 0:
                p.active = False
                p.hidden_reason = f"Auto-retired: no sale in {c['pb_retire_days']} days (Playbook). Switch it on again any time."
                try:
                    Y._push_visible(rt, p, False)
                except Exception:
                    pass
                stats["retired"] += 1
                agent_event(db, "playbook", f"'{p.title}' band kiya: {c['pb_retire_days']} din mein ek bhi sale nahi", "info")
            db.flush()
        except StoreError as e:
            stats["errors"] += 1
            rt.alert(db, level="warning", source="store", dedupe_key=f"pb-{p.id}", message=f"Playbook could not update the price of '{p.title}': {e}")
        except Exception:
            stats["errors"] += 1
            log.exception("playbook failed for product %s", p.id)
    stats["changes"] = stats["repriced"] + stats["restored"] + stats["switched"] + stats["retired"]
    if stats["changes"]:
        agent_event(db, "playbook", f"Playbook: {stats['repriced']} price badle, {stats['restored']} normal par wapas, {stats['switched']} sasta supplier, {stats['retired']} band", "ok")
    S.mark_run(db, "playbook_run", f"{stats['changes']} changes")
    db.commit()
    return stats


def _switch_if_cheaper(db: Session, rt, p: Product, pct: D) -> bool:
    try:
        main = rt.supplier_for(p.supplier_connection_id).get_product_info(p.supplier_vid)
        other = rt.supplier_for(p.backup_connection_id).get_product_info(p.backup_vid)
    except Exception:
        return False
    if other.stock <= 0:
        return False
    if D(other.price) >= D(main.price) * (1 - pct / 100):
        return False
    p.supplier_connection_id, p.backup_connection_id = p.backup_connection_id, p.supplier_connection_id
    p.supplier_vid, p.backup_vid = p.backup_vid, p.supplier_vid
    p.supplier_price = D(other.price)
    agent_event(db, "playbook", f"'{p.title}': sasta supplier chuna ({D(other.price)} vs {D(main.price)})", "ok")
    return True
