"""Strategy agent: sell each product in the country where it sells for the most, undercut the local shelf price, keep a profit.

Example: a fan costs 2000 rupees (about $24) in India and sells for $2000 in the USA. With "undercut by 25%" we list it at $1500 in the US
and ship it from India. All maths is in USD:

    landed cost  = (supplier cost + shipping to that country) * (1 + import duty)
    target price = shelf price there * (1 - undercut)
    floor price  = the lowest price that still leaves the minimum profit after VAT and payment fees
    our price    = the higher of the two, and it must still be cheaper than the shelf price by the minimum gap

Safety: an exchange rate that is only a seed estimate, a stale price observation, or an absurd gap that came from a scraper (not from you)
stops the autopilot and asks for a human. Nothing here ever sells below the floor price.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from ..adapters.base import StoreError
from ..db import utcnow
from ..models import Market, MarketPrice, PriceHistory, Product, StrategyRec
from . import fx
from . import settings_service as S
from .agents import event as agent_event
from .outbound import emit
from .pricing import sell_price

log = logging.getLogger("cheapedeal.strategy")

DEFAULTS = {
    "strategy_autopilot": "false",     # true = the agent changes real prices by itself
    "strategy_multi_market": "false",  # true = also sell in every other profitable market that has its own store, each at its own price
    "strategy_auto_list": "true",      # true = the agent also lists the product on the market's store when it picks that market
    "strategy_mode": "undercut",       # undercut | max_profit | aggressive
    "strategy_undercut_pct": "25",
    "strategy_min_profit_pct": "25",   # profit after shipping, duty, VAT and fees, as % of the price without VAT
    "strategy_min_gap_pct": "5",       # we must be at least this much cheaper than the shelf price there
    "strategy_max_age_days": "14",     # older price observations are not acted on
    "strategy_sanity_ratio": "30",     # scraper-fed prices more than this many times our landed cost wait for a human
}
MODES = {"undercut": "Undercut by the set %", "max_profit": "Just below the market (2%)", "aggressive": "Aggressive (1.5x the undercut, max 60%)"}

SEED_MARKETS = [  # code, name, currency, shipping USD, duty %, VAT %  (starting points: edit them to your real numbers)
    ("US", "United States", "USD", "12", "0", "0"), ("GB", "United Kingdom", "GBP", "12", "0", "20"),
    ("DE", "Germany / EU", "EUR", "12", "0", "19"), ("AU", "Australia", "AUD", "14", "0", "10"),
    ("CA", "Canada", "CAD", "13", "0", "5"), ("AE", "United Arab Emirates", "AED", "10", "5", "5"),
    ("IN", "India", "INR", "5", "0", "18"),
]

D = Decimal
CENT = D("0.01")
RANK = {"viable": 0, "applied": 0, "needs_check": 1, "needs_rate": 2, "stale": 3, "not_viable": 4, "no_data": 5}


def _q(v) -> Decimal:
    return D(v).quantize(CENT, rounding=ROUND_HALF_UP)


# ---- settings ------------------------------------------------------------------------------
@dataclass
class Params:
    autopilot: bool = False
    auto_list: bool = True
    mode: str = "undercut"
    undercut: Decimal = D("25")
    min_profit: Decimal = D("25")
    min_gap: Decimal = D("5")
    max_age_days: int = 14
    sanity_ratio: Decimal = D("30")
    ending: str = ""
    multi_market: bool = False


def params(db: Session) -> Params:
    def g(k):
        return S.get(db, k, DEFAULTS[k]) or DEFAULTS[k]

    def num(k):
        try:
            return D(g(k))
        except Exception:
            return D(DEFAULTS[k])
    mode = g("strategy_mode")
    return Params(autopilot=g("strategy_autopilot") == "true", auto_list=g("strategy_auto_list") == "true",
                  mode=mode if mode in MODES else "undercut", undercut=num("strategy_undercut_pct"), min_profit=num("strategy_min_profit_pct"),
                  min_gap=num("strategy_min_gap_pct"), max_age_days=int(num("strategy_max_age_days")),
                  sanity_ratio=num("strategy_sanity_ratio"), ending=S.get(db, "price_ending"),
                  multi_market=g("strategy_multi_market") == "true")


def save_settings(db: Session, form: dict) -> list[str]:
    errs: list[str] = []
    clean = {"strategy_autopilot": "true" if form.get("strategy_autopilot") in ("on", "true", "1") else "false",
             "strategy_auto_list": "true" if form.get("strategy_auto_list") in ("on", "true", "1") else "false",
             "strategy_multi_market": "true" if form.get("strategy_multi_market") in ("on", "true", "1") else "false"}
    if form.get("strategy_mode") not in MODES:
        errs.append("Choose a pricing style from the list")
    else:
        clean["strategy_mode"] = form["strategy_mode"]
    rules = {"strategy_undercut_pct": ("Undercut %", D("0"), D("90")), "strategy_min_profit_pct": ("Minimum profit %", D("0"), D("90")),
             "strategy_min_gap_pct": ("Minimum gap %", D("0"), D("90")), "strategy_max_age_days": ("Maximum age (days)", D("1"), D("365")),
             "strategy_sanity_ratio": ("Sanity ratio", D("1"), D("100000"))}
    for k, (label, lo, hi) in rules.items():
        try:
            v = D(str(form.get(k, "")).strip())
            if v < lo or v > hi:
                raise ValueError
            clean[k] = str(v.normalize() if v == v.to_integral() else v)
        except Exception:
            errs.append(f"{label} must be a number from {lo} to {hi}")
    if not errs:
        for k, v in clean.items():
            S.set_value(db, k, v)
    return errs


# ---- markets and observations --------------------------------------------------------------
def seed_markets(db: Session) -> None:
    fx.ensure_seed(db)
    if db.query(Market).count() == 0:
        for code, name, cur, ship, duty, tax in SEED_MARKETS:
            db.add(Market(code=code, name=name, currency=cur, shipping_usd=D(ship), duty_pct=D(duty), tax_pct=D(tax)))
    db.flush()


def save_market(db: Session, *, mid: int | None, code: str, name: str, currency: str, shipping: str, duty: str, tax: str, fee: str,
                store_connection: str, enabled: bool, per_kg: str = "0") -> Market:
    code = (code or "").strip().upper()
    if not (1 <= len(code) <= 8 and code.isalnum()):
        raise ValueError("Market code: letters/numbers only, like US")
    cur = fx.norm(currency)
    if not (len(cur) == 3 and cur.isalpha()):
        raise ValueError("Currency code has 3 letters, like USD")
    nums = {}
    for label, v, hi in (("Shipping", shipping, D("10000")), ("Import duty", duty, D("200")), ("VAT/GST", tax, D("100")), ("Payment fee", fee, D("50")), ("Shipping per kg", per_kg, D("1000"))):
        try:
            x = D((v or "0").strip())
        except Exception:
            raise ValueError(f"{label} must be a number")
        if x < 0 or x > hi:
            raise ValueError(f"{label} must be between 0 and {hi}")
        nums[label] = x
    m = db.get(Market, mid) if mid else None
    dup = db.query(Market).filter(Market.code == code).first()
    if dup is not None and (m is None or dup.id != m.id):
        raise ValueError(f"Market {code} already exists")
    if m is None:
        m = Market(code=code)
        db.add(m)
    m.code, m.name, m.currency = code, (name or code).strip()[:80], cur
    m.shipping_usd, m.duty_pct, m.tax_pct, m.fee_pct = nums["Shipping"], nums["Import duty"], nums["VAT/GST"], nums["Payment fee"]
    m.ship_per_kg_usd = nums["Shipping per kg"]
    m.store_connection_id = int(store_connection) if (store_connection or "").strip().isdigit() and int(store_connection) > 0 else None
    m.enabled = enabled
    if fx.get(db, cur) is None:
        raise ValueError(f"Add an exchange rate for {cur} first (Strategy > Exchange rates)")
    db.flush()
    return m


def find_product(db: Session, ref: str) -> Product:
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("no product given")
    if ref.isdigit():
        p = db.get(Product, int(ref))
        if p:
            return p
    rows = db.query(Product).filter(Product.sku == ref).all() if ref else []
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        raise ValueError(f"SKU {ref} matches {len(rows)} products; use the product id")
    raise ValueError(f"no product with id or SKU '{ref}'")


def record_price(db: Session, product: Product, market: Market, local_price, *, url: str = "", source: str = "manual") -> MarketPrice:
    try:
        v = D(str(local_price).strip().replace(",", ""))
    except Exception:
        raise ValueError(f"'{local_price}' is not a price")
    if v <= 0 or v > D("1000000000"):
        raise ValueError("The price must be above 0")
    if source not in ("manual", "csv", "api"):
        source = "manual"
    row = db.query(MarketPrice).filter(MarketPrice.product_id == product.id, MarketPrice.market_id == market.id).first()
    if row is None:
        row = MarketPrice(product_id=product.id, market_id=market.id, local_price=_q(v))
        db.add(row)
    elif D(row.local_price) > 0 and _q(v) <= D(row.local_price) * D("0.8"):
        pct = int((1 - _q(v) / D(row.local_price)) * 100)  # a competitor getting much cheaper is news: you may need to react
        agent_event(db, "strategist", f"Price drop in {market.name}: '{product.title}' fell {pct}% ({row.local_price} -> {_q(v)} {market.currency})", "warn")
    row.local_price, row.url, row.source, row.observed_at = _q(v), (url or "").strip()[:500], source, utcnow()
    db.flush()
    return row


def import_prices_csv(db: Session, text: str, source: str = "csv") -> tuple[int, list[str]]:
    """Columns: sku (or product_id), market, price, url (optional). Returns (saved, errors)."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "market" not in rows[0] or "price" not in rows[0] or not ("sku" in rows[0] or "product_id" in rows[0]):
        raise ValueError("The first row must be a header: sku (or product_id), market, price, url (optional)")
    ok, errs = 0, []
    for n, r in enumerate(rows, start=2):
        try:
            p = find_product(db, r.get("product_id") or r.get("sku") or "")
            m = db.query(Market).filter(Market.code == (r.get("market") or "").strip().upper()).first()
            if m is None:
                raise ValueError(f"unknown market '{r.get('market')}'")
            record_price(db, p, m, r.get("price") or "", url=r.get("url") or "", source=source)
            ok += 1
        except ValueError as e:
            errs.append(f"row {n}: {e}")
    return ok, errs


# ---- the maths -----------------------------------------------------------------------------
@dataclass
class Eval:
    market: Market
    obs: MarketPrice | None
    status: str = "no_data"
    reason: str = ""
    ref_local: Decimal = D("0")
    ref_usd: Decimal = D("0")
    landed_usd: Decimal = D("0")
    floor_usd: Decimal = D("0")
    price_usd: Decimal = D("0")
    price_local: Decimal = D("0")
    profit_usd: Decimal = D("0")
    profit_pct: Decimal = D("0")
    ratio: Decimal = D("0")
    warnings: list = field(default_factory=list)


def _undercut_for(mode: str, base: Decimal) -> Decimal:
    if mode == "max_profit":
        return D("2")
    if mode == "aggressive":
        return min(base * D("1.5"), D("60"))
    return base


def evaluate(db: Session, p: Product, m: Market, obs: MarketPrice | None, pr: Params, *, undercut: Decimal | None = None) -> Eval:
    ev = Eval(market=m, obs=obs)
    if obs is None:
        ev.reason = "No price entered for this market yet"
        return ev
    cost = D(p.supplier_price)
    if cost <= 0:
        ev.reason = "The product has no supplier cost yet (run a stock sync)"
        return ev
    try:
        rate = fx.rate(db, m.currency)
    except fx.FxError as e:
        ev.status, ev.reason = "needs_rate", str(e)
        return ev
    ev.ref_local = D(obs.local_price)
    ev.ref_usd = _q(ev.ref_local / rate)
    ship = D(m.shipping_usd) + D(getattr(m, 'ship_per_kg_usd', 0) or 0) * D(getattr(p, 'weight_kg', 0) or 0)
    ev.landed_usd = _q((cost + ship) * (1 + D(m.duty_pct) / 100))
    ev.ratio = (ev.ref_usd / ev.landed_usd).quantize(D("0.1")) if ev.landed_usd > 0 else D("0")
    tax, fee = D(m.tax_pct) / 100, D(m.fee_pct) / 100
    denom = (1 - pr.min_profit / 100) / (1 + tax) - fee
    if denom <= 0:
        ev.status, ev.reason = "not_viable", "VAT, fees and the minimum profit together leave no room: lower the minimum profit % or the fees"
        return ev
    ev.floor_usd = _q(ev.landed_usd / denom)
    u = _undercut_for(pr.mode, pr.undercut if undercut is None else undercut)
    target = ev.ref_usd * (1 - u / 100)
    price = sell_price(max(target, ev.floor_usd), D("0"), pr.ending)
    if price < ev.floor_usd:  # rounding can only go up, but never trust that
        price = ev.floor_usd
    ev.price_usd = price
    ev.price_local = _q(price * rate)
    net = price / (1 + tax)
    ev.profit_usd = _q(net - ev.landed_usd - price * fee)
    ev.profit_pct = _q(ev.profit_usd / net * 100) if net > 0 else D("0")
    if price > ev.ref_usd * (1 - pr.min_gap / 100):
        ev.status = "not_viable"
        ev.reason = (f"Cannot beat the market by {pr.min_gap.normalize()}% and still keep {pr.min_profit.normalize()}% profit "
                     f"(we need at least ${ev.floor_usd} here, the market price is ${ev.ref_usd})")
        return ev
    if ev.ratio > pr.sanity_ratio:
        ev.warnings.append(f"The market price is {ev.ratio}x our cost: double-check it is really the same product")
    if utcnow() - obs.observed_at > timedelta(days=pr.max_age_days):
        ev.status, ev.reason = "stale", f"The market price was last checked more than {pr.max_age_days} days ago: refresh it"
        return ev
    if not fx.confirmed(db, m.currency):
        ev.status, ev.reason = "needs_rate", f"The {m.currency} exchange rate is only an estimate: confirm it under Exchange rates"
        return ev
    if ev.ratio > pr.sanity_ratio and obs.source == "api":
        ev.status, ev.reason = "needs_check", ev.warnings[0] + " (it came from a scraper, so a person must confirm it)"
        return ev
    ev.status = "viable"
    ev.reason = f"Sell at ${price} ({ev.price_local} {m.currency}) vs ${ev.ref_usd} there: ${ev.profit_usd} profit per unit"
    return ev


def plan(db: Session, p: Product, pr: Params | None = None) -> list[Eval]:
    """Evaluate the product in every enabled market, best first."""
    pr = pr or params(db)
    obs = {o.market_id: o for o in db.query(MarketPrice).filter(MarketPrice.product_id == p.id)}
    out = [evaluate(db, p, m, obs.get(m.id), pr) for m in db.query(Market).filter(Market.enabled.is_(True)).order_by(Market.id)]
    out.sort(key=lambda e: (RANK[e.status], -e.profit_usd))
    return out


# ---- applying ------------------------------------------------------------------------------
def _rec_for(db: Session, p: Product) -> StrategyRec:
    r = db.query(StrategyRec).filter(StrategyRec.product_id == p.id).first()
    if r is None:
        r = StrategyRec(product_id=p.id)
        db.add(r)
        db.flush()  # sessions here do not autoflush: make the row visible to the next lookup
    return r


def _fill(rec: StrategyRec, ev: Eval | None, status: str | None = None, reason: str | None = None) -> None:
    rec.computed_at = utcnow()
    if ev is None:
        rec.market_id, rec.status, rec.reason = None, status or "no_data", reason or "No market prices entered"
        rec.price_usd = rec.price_local = rec.reference_usd = rec.profit_usd = rec.profit_pct = D("0")
        return
    rec.market_id, rec.status = ev.market.id, status or ev.status
    rec.price_usd, rec.price_local, rec.reference_usd = ev.price_usd, ev.price_local, ev.ref_usd
    rec.profit_usd, rec.profit_pct = ev.profit_usd, ev.profit_pct
    rec.reason = (reason if reason is not None else ev.reason + ("; " + "; ".join(ev.warnings) if ev.warnings and ev.status == "viable" else ""))[:300]


def apply(db: Session, rt, p: Product, ev: Eval, *, actor: str = "strategy", auto_list: bool = True) -> tuple[bool, str]:
    """Put the strategy price live. Store first, database second: a store failure leaves everything as it was. Never raises."""
    from . import sync as Y
    from .catalog import list_on_store
    if ev.status != "viable":
        return False, ev.reason or "This price is not safe to apply"
    from .guard import blocked_reason
    why = blocked_reason(db, p.title, p.category)
    if why:
        return False, why
    new, old = ev.price_usd, D(p.selling_price)
    try:
        Y._push_price(rt, p, new)
    except StoreError as e:
        rt.alert(db, level="warning", source="strategy", dedupe_key=f"st-{p.id}", message=f"Could not push the strategy price for '{p.title}': {e}")
        return False, f"The store refused the price change: {e}"
    p.selling_price, p.strategy_managed, p.target_market_id = new, True, ev.market.id
    db.add(PriceHistory(product_id=p.id, supplier_price=p.supplier_price, selling_price=new))
    note = ""
    if auto_list and ev.market.store_connection_id:
        ok, msg = list_on_store(db, rt, p, ev.market.store_connection_id)
        note = f"; {msg}" if ok else f"; could not list on {ev.market.name} store ({msg}) - will retry"
    rec = _rec_for(db, p)
    _fill(rec, ev, "applied")
    rec.applied_at = utcnow()
    agent_event(db, "strategist", f"'{p.title}' -> sell in {ev.market.name} at ${new} (market ${ev.ref_usd}, profit ${ev.profit_usd}){note}", "ok")
    emit(db, "product.strategy_applied", {"product_id": p.id, "title": p.title, "market": ev.market.code, "price_usd": str(new),
                                           "market_price_usd": str(ev.ref_usd), "profit_usd": str(ev.profit_usd), "previous_price": str(old)})
    return True, f"Priced at ${new} for {ev.market.name}"


def apply_extra_markets(db: Session, rt, p: Product, evs: list[Eval], best: Eval | None) -> int:
    """Multi-country: besides the best market, every other VIABLE market that has its own store gets the product at its own price.
    A store whose market stopped being viable goes back to the normal product price. Returns how many store prices changed. Never raises."""
    from .catalog import list_on_store
    n = 0
    live = {}
    for ev in evs:
        sid = ev.market.store_connection_id
        if ev.status == "viable" and sid and (best is None or ev.market.id != best.market.id) and sid != (best.market.store_connection_id if best else None):
            if sid not in live or ev.profit_usd > live[sid].profit_usd:
                live[sid] = ev
    for sid, ev in live.items():
        try:
            ok, _ = list_on_store(db, rt, p, sid, price=ev.price_usd)
            if ok:
                n += 1
        except Exception:
            log.exception("multi-market listing failed for %s", p.id)
    for l in list(p.listings):  # stores that no longer have a profitable market of their own: back to the product price
        if l.price_usd is not None and l.connection_id not in live and l.status == "listed" and l.remote_id:
            try:
                rt.store_for(l.connection_id).set_product_price(l.remote_id, D(p.selling_price))
                l.price_usd = None
                n += 1
            except StoreError:
                pass
    return n


def revert(db: Session, rt, p: Product, *, reason: str) -> bool:
    """Go back to the normal markup price (used when the price gap disappears, or when you press Revert)."""
    from . import sync as Y
    price = sell_price(D(p.supplier_price), S.get_decimal(db, "markup_pct"), S.get(db, "price_ending")) if D(p.supplier_price) > 0 else D(p.selling_price)
    try:
        if price != D(p.selling_price):
            Y._push_price(rt, p, price)
    except StoreError as e:
        rt.alert(db, level="warning", source="strategy", dedupe_key=f"strv-{p.id}", message=f"Could not revert the price of '{p.title}': {e}")
        return False
    if price != D(p.selling_price):
        db.add(PriceHistory(product_id=p.id, supplier_price=p.supplier_price, selling_price=price))
    p.selling_price, p.strategy_managed, p.target_market_id = price, False, None
    agent_event(db, "strategist", f"'{p.title}' back to the normal markup price ${price}: {reason}", "warn")
    return True


# ---- the job -------------------------------------------------------------------------------
def run(db: Session, rt, *, apply_changes: bool | None = None) -> dict:
    """Re-evaluate every product that has market prices. apply_changes=None follows the autopilot setting."""
    stats = {"analysed": 0, "viable": 0, "applied": 0, "reverted": 0, "needs_you": 0, "errors": 0, "paused": False, "fx": "", "extra_stores": 0}
    if S.get_bool(db, "paused"):
        stats["paused"] = True
        return stats
    seed_markets(db)
    r = fx.refresh_if_due(db)
    if r is not None:
        stats["fx"] = f"{r['updated']} rates refreshed" if r["ok"] else r["error"]
        if not r["ok"]:
            agent_event(db, "strategist", f"{r['error']}. Keeping the saved rates.", "warn")
    db.commit()
    pr = params(db)
    auto = pr.autopilot if apply_changes is None else apply_changes
    ids = [i for (i,) in db.query(MarketPrice.product_id).distinct().all()]
    for pid in ids:
        p = db.get(Product, pid)
        if p is None:
            continue
        try:
            with db.begin_nested():
                evs = plan(db, p, pr)
                best = evs[0] if evs else None
                rec = _rec_for(db, p)
                stats["analysed"] += 1
                if best is None:
                    _fill(rec, None, "no_data", "No enabled market has a price")
                    continue
                locked = p.manual_override or not p.active
                if best.status == "viable":
                    stats["viable"] += 1
                    same = p.strategy_managed and p.target_market_id == best.market.id and _close(D(p.selling_price), best.price_usd)
                    if same:
                        _fill(rec, best, "applied")
                    elif locked:
                        _fill(rec, best, "viable", "Locked (manual price or inactive): " + best.reason)
                    elif auto:
                        ok, msg = apply(db, rt, p, best, auto_list=pr.auto_list)
                        stats["applied" if ok else "errors"] += 1
                        if not ok:
                            _fill(rec, best, "viable", "Could not apply: " + msg)
                    else:
                        _fill(rec, best)
                else:
                    _fill(rec, best)
                    if best.status in ("needs_check", "needs_rate", "stale"):
                        stats["needs_you"] += 1
                    if auto and not locked and p.strategy_managed and best.status == "not_viable":
                        if revert(db, rt, p, reason="the price gap no longer leaves a safe profit"):
                            stats["reverted"] += 1
                if auto and pr.multi_market and not locked and evs:
                    stats["extra_stores"] += apply_extra_markets(db, rt, p, evs, best if (best and best.status == "viable") else None)
        except Exception as e:  # one broken product must never stop the others
            stats["errors"] += 1
            log.exception("strategy failed for product %s", pid)
            rt.alert(db, level="warning", source="strategy", dedupe_key=f"stx-{pid}", message=f"Strategy could not evaluate '{p.title}': {e}")
        db.commit()
    S.mark_run(db, "strategy_run", f"{stats['analysed']} analysed, {stats['applied']} applied")
    db.commit()
    return stats


def _close(a: Decimal, b: Decimal) -> bool:
    """Prices within 0.5% count as the same, so small FX wobble does not cause a price change every run."""
    return abs(a - b) <= max(D("0.01"), b * D("0.005"))
