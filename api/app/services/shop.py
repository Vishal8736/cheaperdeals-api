"""The public website's brain: what it shows, how an order is priced, and how a paid order enters the engine.

Rules that keep it safe:
  * The browser never sends prices. Every price, shipping and total is recomputed here from the Products table (USD) and the exchange
    rates (Strategy page), then shown in the customer's chosen currency.
  * An order only enters the engine (and so only spends supplier money) after it is PAID: gateway signature / admin Mark paid
    check, or you pressed "Mark paid" after the money arrived.
  * Nothing here talks to the database unless a request needs it, so /ping and cached product lists cost the server nothing.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from decimal import Decimal

from sqlalchemy.orm import Session

from ..adapters.base import NormalizedItem, NormalizedOrder
from ..models import Order, Product, ShopOrder, Subscriber
from . import fx, payments
from .shopcfg import DEFAULTS, SYMBOLS, configure, get, money, read_secret, save_secret, symbol  # noqa: F401  (re-exported for the routers)

MAX_LINES, MAX_QTY = 20, 20


# ---- what the website shows ----------------------------------------------------------------------
_cache: dict[str, tuple[float, object]] = {}
CACHE_SECONDS = 30


def drop_cache() -> None:
    _cache.clear()


def allowed_currencies(db: Session) -> list[str]:
    """Currencies a customer may choose: the ones listed on the Website page that also have an exchange rate."""
    if fx.get(db, "INR") is None:
        fx.ensure_seed(db)
    want = [c.strip().upper() for c in get(db, "shop_currencies").split(",") if c.strip()]
    out = [c for c in dict.fromkeys(want) if fx.get(db, c) is not None]
    return out or ["USD"]


def pick_currency(db: Session, wanted: str = "") -> str:
    ok = allowed_currencies(db)
    w = (wanted or "").strip().upper()
    if w in ok:
        return w
    d = (get(db, "shop_currency") or "USD").upper()
    return d if d in ok else ok[0]


def public_config(db: Session) -> dict:
    cur = pick_currency(db)
    rates = {c: {"rate": str(fx.rate(db, c)), "symbol": symbol(c)} for c in allowed_currencies(db)}
    return {"name": get(db, "shop_name"), "tagline": get(db, "shop_tagline"), "announce": get(db, "shop_announce"), "currency": cur, "rates": rates,
            "ship_flat_usd": str(money(get(db, "shop_ship_flat") or 0)), "free_over_usd": str(money(get(db, "shop_free_over") or 0)),
            "methods": payments.options(db), "razorpay_key": get(db, "razorpay_key_id") if "razorpay" in payments.enabled(db) else "",
            "countries": [c.strip().upper() for c in get(db, "shop_countries").split(",") if c.strip()],
            "support_email": get(db, "shop_support_email"), "delivery_days": get(db, "shop_delivery_days"), "address": get(db, "shop_address"),
            "open": get(db, "shop_on").lower() not in ("false", "0", "no", "off")}


def sellable(db: Session):
    return db.query(Product).filter(Product.active.is_(True), Product.auto_hidden.is_(False), Product.listing_status == "listed",
                                    Product.stock > 0, Product.selling_price > 0)


def _item(p: Product) -> dict:
    return {"id": p.id, "title": p.title, "price_usd": str(money(p.selling_price)), "image": p.image_url, "category": p.category or "",
            "description": (p.description or "")[:600], "stock": min(int(p.stock), 20)}


def products(db: Session) -> list[dict]:
    hit = _cache.get("products")
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]  # type: ignore[return-value]
    from . import support
    rt_ = support.ratings(db)
    rows = [{**_item(p), "rating": rt_.get(p.id, (0, 0))[0], "reviews": rt_.get(p.id, (0, 0))[1]} for p in sellable(db).order_by(Product.listed_at.desc(), Product.id.desc()).limit(300).all()]
    _cache["products"] = (time.monotonic(), rows)
    return rows


# ---- pricing an order ----------------------------------------------------------------------------
def quote(db: Session, lines, currency: str = "") -> dict:
    """lines = [{id, qty}] from the browser. Prices are taken from the database (USD) and converted. Raises ValueError (customer-safe text)."""
    if not isinstance(lines, list) or not lines:
        raise ValueError("Cart khali hai.")
    if len(lines) > MAX_LINES:
        raise ValueError("Cart mein bahut zyada items hain.")
    merged: dict[int, int] = {}
    for ln in lines:
        try:
            pid, qty = int(ln.get("id")), int(ln.get("qty", 1))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Cart ka item samajh nahi aaya.")
        if qty < 1 or qty > MAX_QTY:
            raise ValueError(f"Ek item ki quantity 1 se {MAX_QTY} ke beech honi chahiye.")
        merged[pid] = merged.get(pid, 0) + qty
    cur = pick_currency(db, currency)
    out, sub_usd, sub = [], Decimal("0"), Decimal("0")
    for pid, qty in merged.items():
        p = sellable(db).filter(Product.id == pid).first()
        if p is None:
            raise ValueError("Cart ka ek item ab available nahi hai. Cart refresh karo.")
        if qty > int(p.stock):
            raise ValueError(f"'{p.title}' ke sirf {int(p.stock)} piece bache hain.")
        out.append((p, qty))
        sub_usd += money(p.selling_price) * qty
        sub += fx.from_usd(db, p.selling_price, cur) * qty
    flat, free_over = money(get(db, "shop_ship_flat") or 0), money(get(db, "shop_free_over") or 0)
    ship_usd = Decimal("0") if (free_over > 0 and sub_usd >= free_over) else flat
    ship = fx.from_usd(db, ship_usd, cur)
    return {"lines": out, "currency": cur, "symbol": symbol(cur), "rate": fx.rate(db, cur), "sub_usd": money(sub_usd), "ship_usd": money(ship_usd),
            "total_usd": money(sub_usd + ship_usd), "subtotal": money(sub), "shipping": money(ship), "total": money(sub + ship)}


def quote_public(q: dict) -> dict:
    return {"currency": q["currency"], "symbol": q["symbol"], "subtotal": str(q["subtotal"]), "shipping": str(q["shipping"]), "total": str(q["total"])}


# ---- creating and paying ---------------------------------------------------------------------------
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
_hits: dict[str, list[float]] = {}


def rate_limited(who: str, per_minute: int = 8) -> bool:
    now = time.monotonic()
    hits = [t for t in _hits.get(who, []) if now - t < 60]
    hits.append(now)
    _hits[who] = hits
    if len(_hits) > 5000:  # forget old visitors
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
            _hits.pop(k, None)
    return len(hits) > per_minute


def _clean(v, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def number(so: ShopOrder) -> str:
    return f"W{1000 + so.id}"


def create_order(db: Session, data: dict, *, redirect_base: str = "") -> tuple[ShopOrder, dict]:
    """Validate, price on the server, prepare the chosen payment. Returns (order, payment info). Raises ValueError with a customer-safe message."""
    if get(db, "shop_on").lower() in ("false", "0", "no", "off"):
        raise ValueError("Dukaan abhi band hai. Thodi der baad aana.")
    q = quote(db, data.get("items"), str(data.get("currency", "")))
    name, email, phone = _clean(data.get("name"), 120), _clean(data.get("email"), 254).lower(), _clean(data.get("phone"), 30)
    a1, a2, city = _clean(data.get("address1"), 200), _clean(data.get("address2"), 200), _clean(data.get("city"), 100)
    state, zipc, cc = _clean(data.get("state"), 100), _clean(data.get("zip"), 20), _clean(data.get("country"), 2).upper()
    if len(name) < 2 or not a1 or not city or not zipc:
        raise ValueError("Naam, address, city aur pin/zip code bharo.")
    if not _EMAIL.match(email):
        raise ValueError("Email sahi nahi lag raha.")
    if len(re.sub(r"\D", "", phone)) < 7:
        raise ValueError("Phone number sahi daalo (delivery ke liye zaroori hai).")
    if cc not in [c.strip().upper() for c in get(db, "shop_countries").split(",") if c.strip()]:
        raise ValueError("Is desh mein abhi delivery nahi hai.")
    method = str(data.get("method") or payments.enabled(db)[0])
    items = [{"id": p.id, "title": p.title, "qty": n, "price": str(money(p.selling_price)), "store_product_id": p.store_product_id, "sku": p.sku} for p, n in q["lines"]]
    so = ShopOrder(token=secrets.token_urlsafe(16), pay_mode=method, customer_name=name, email=email, phone=phone, address1=a1, address2=a2,
                   city=city, state=state, zip_code=zipc, country_code=cc, items_json=json.dumps(items), subtotal=q["subtotal"], shipping=q["shipping"],
                   total=q["total"], currency=q["currency"], total_usd=q["total_usd"], fx_rate=q["rate"])
    db.add(so)
    db.flush()
    try:
        pay = payments.prepare(db, so, method, redirect_url=f"{redirect_base}/#track={so.token}" if redirect_base else "")
    except ValueError:
        db.rollback()
        raise
    so.pay_data = json.dumps(pay)
    db.flush()
    return so, pay


def mark_paid(db: Session, rt, so: ShopOrder, payment_id: str = "", by: str = "payment") -> tuple[Order | None, str]:
    """Pending -> paid -> a normal engine Order. Safe to call twice (webhook + browser both arrive): the second call does nothing."""
    from . import orders as O
    if so.status in ("cancelled", "refunded"):
        return None, "Ye order cancel/refund ho chuka hai."
    if so.status == "paid" and so.engine_order_id:
        return db.get(Order, so.engine_order_id), "already paid"
    items = json.loads(so.items_json or "[]")
    norm = NormalizedOrder(
        store_order_id=f"web-{so.id}", number=number(so), status="paid", paid=True, customer_name=so.customer_name, email=so.email, phone=so.phone,
        address1=so.address1, address2=so.address2, city=so.city, state=so.state, zip_code=so.zip_code, country_code=so.country_code,
        country=so.country_code, total=so.total_usd, currency="USD",  # the engine always works in USD
        items=[NormalizedItem(store_product_id=i.get("store_product_id", ""), sku=i.get("sku", ""), title=i["title"], quantity=int(i["qty"]), unit_price=Decimal(i["price"])) for i in items])
    order, _created, reason = O.intake(db, norm, source="website")
    if order is None:
        return None, reason
    so.status, so.payment_id, so.engine_order_id = "paid", (payment_id or by)[:128], order.id
    db.flush()
    try:
        rt.alert(db, level="info", source="website", message=f"Website order {number(so)} paid: {so.total} {so.currency} ({len(items)} item)", order_id=order.id)
    except Exception:
        pass
    _confirm_email(db, so)
    return order, "ok"


def _confirm_email(db: Session, so: ShopOrder) -> None:
    """Customer gets a short receipt from YOUR mailbox, only if you connected one and turned customer emails on."""
    try:
        from . import mailer
        if not (mailer.configured(db) and mailer.customer_enabled(db)):
            return
        name = get(db, "shop_name")
        body = (f"Hi {so.customer_name},\n\nAapka order {number(so)} mil gaya hai aur payment confirm ho gayi ({so.total} {so.currency}).\n"
                f"Hum ab aapka saman bhej rahe hain. Status dekhne ke liye track page par order number {number(so)} aur apna email daalo.\n\nShukriya,\n{name}")
        mailer.send_logged(db, so.email, f"{name}: order {number(so)} confirmed", body, "system")
    except Exception:
        pass


def cancel(db: Session, so: ShopOrder) -> bool:
    if so.status != "pending":
        return False
    so.status = "cancelled"
    db.flush()
    return True


def refund(db: Session, rt, so: ShopOrder) -> tuple[bool, str]:
    """Paid website order -> refunded. Also stops the engine order if the supplier has not been paid yet."""
    from . import orders as O
    if so.status != "paid":
        return False, "Sirf paid order ka refund ho sakta hai."
    ok, msg = payments.refund(db, so)
    if not ok:
        return False, msg
    so.status = "refunded"
    note = ""
    if so.engine_order_id:
        res = O.cancel(db, rt, so.engine_order_id, actor="refund")
        if res.get("supplier_order_placed"):
            note = " CJ par order pehle hi lag chuka hai: CJ dashboard se bhi cancel karo."
        elif res.get("outcome") == "not_cancellable":
            note = " Order pehle hi ship ho chuka hai, supplier ko nahi roka ja sakta."
    db.flush()
    return True, msg + note


def subscribe(db: Session, email: str) -> bool:
    email = _clean(email, 254).lower()
    if not _EMAIL.match(email):
        return False
    if db.query(Subscriber).filter(Subscriber.email == email).first() is None:
        db.add(Subscriber(email=email))
        db.flush()
    return True


def track(db: Session, number_or_token: str, email: str = "") -> dict | None:
    """Customer-facing status. Found by the secret token, or by order number + the e-mail used on the order."""
    q = db.query(ShopOrder)
    key = (number_or_token or "").strip()
    so = q.filter(ShopOrder.token == key).first()
    if so is None and re.fullmatch(r"[Ww]\d{4,}", key) and email:
        cand = q.filter(ShopOrder.id == int(key[1:]) - 1000).first()
        if cand and cand.email == email.strip().lower():
            so = cand
    if so is None:
        return None
    stage = {"pending": "Payment ka intezar", "cancelled": "Cancelled", "refunded": "Refund ho gaya"}.get(so.status, "Order mil gaya")
    out = {"number": number(so), "status": so.status, "total": str(so.total), "currency": so.currency, "symbol": symbol(so.currency), "stage": stage,
           "tracking": [], "token": so.token, "method": so.pay_mode}
    if so.status == "pending":
        out["pay"] = json.loads(so.pay_data or "{}")
    if so.engine_order_id and so.status == "paid":
        o = db.get(Order, so.engine_order_id)
        if o is not None:
            out["stage"] = {"shipped": "Bhej diya gaya", "delivered": "Deliver ho gaya", "submitted_to_supplier": "Supplier ko bhej diya", "supplier_confirmed": "Supplier ne confirm kiya"}.get(o.status, "Taiyari chal rahi hai")
            out["tracking"] = [{"status": t.status, "number": t.tracking_number, "carrier": t.carrier, "when": t.created_at.isoformat()} for t in o.tracking]
    return out


def publish_to_website(db: Session, p: Product) -> bool:
    """Make a product visible on the website (the website is the store: no external listing needed)."""
    if Decimal(p.selling_price) <= 0:
        return False
    if not p.store_product_id or p.store_product_id.startswith("draft-"):
        p.store_product_id = f"web-{p.id}"
    from ..db import utcnow
    p.listing_status, p.listed_at = "listed", p.listed_at or utcnow()
    db.flush()
    drop_cache()
    return True


# ---- background watcher (runs every 2 minutes inside the scheduler) --------------------------------
def watch(db: Session, rt) -> dict:
    """Cancels orders nobody paid for in 48 h, keeps exchange rates fresh, checks the CJ wallet."""
    from datetime import timedelta
    from ..db import utcnow
    res = {"paid": 0, "expired": 0}
    for so in db.query(ShopOrder).filter(ShopOrder.status == "pending", ShopOrder.created_at < utcnow() - timedelta(hours=48)).all():
        so.status = "cancelled"
        res["expired"] += 1
    fx.refresh_if_due(db, 12)
    from . import support
    late = support.overdue(db, 24)
    if late:
        rt.alert(db, level="warning", source="support", message=f"{late} customer ticket 24 ghante se zyada se unanswered hai. Support page dekho.", dedupe_key="tickets-overdue", dedupe_hours=12)
    res["overdue"] = late
    db.flush()
    return res
