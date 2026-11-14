"""Affiliate products: we show them in the shop and send the visitor to the merchant with our tracked link. The merchant pays the commission later, so
there is no cart, no stock and no supplier order for these. Nothing here pretends a sale: we count CLICKS only. Earnings are in the network's own dashboard."""
from __future__ import annotations

import logging
import secrets
from decimal import Decimal

from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Connection, Product
from . import fx
from . import settings_service as S
from .agents import event

log = logging.getLogger("cheapedeal.affiliate")
AGENT = "affiliate"


def config(db: Session) -> dict:
    def num(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(float(S.get(db, key, str(default)) or default))))
        except ValueError:
            return default
    kws = [k.strip() for k in (S.get(db, "aff_keywords", "") or "").replace("\n", ",").split(",") if k.strip()]
    return {"on": S.get(db, "aff_on", "true") == "true", "keywords": kws, "target": num("aff_target", 60, 1, 2000), "batch": num("aff_batch", 12, 1, 100)}


def counts(db: Session) -> dict:
    q = db.query(Product).filter(Product.kind == "affiliate")
    from sqlalchemy import func
    return {"total": q.count(), "live": q.filter(Product.active.is_(True), Product.auto_hidden.is_(False), Product.listing_status == "listed").count(),
            "clicks": int(db.query(func.coalesce(func.sum(Product.clicks), 0)).filter(Product.kind == "affiliate").scalar() or 0)}


def import_offers(db: Session, conn: Connection, offers: list, *, limit: int, merchant: str = "") -> dict:
    """Add new offers as live affiliate products. An offer is skipped (and counted) when it is already imported or its currency has no rate."""
    have = {r[0] for r in db.query(Product.supplier_vid).filter(Product.kind == "affiliate", Product.supplier_connection_id == conn.id).all()}
    made = dup = no_rate = 0
    for o in offers:
        if made >= limit:
            break
        if o.offer_id in have:
            dup += 1
            continue
        try:
            usd = fx.to_usd(db, o.price, o.currency or "USD")
        except fx.FxError:
            no_rate += 1
            continue
        if usd <= 0:
            continue
        have.add(o.offer_id)
        db.add(Product(store_product_id="AFF-" + secrets.token_hex(6), sku=f"aff-{conn.id}-{o.offer_id}"[:64], title=o.title[:255], supplier_vid=o.offer_id[:128],
                       supplier_price=Decimal("0"), selling_price=usd, stock=999, active=True, manual_override=True, supplier_connection_id=conn.id,
                       supplier_product_id=o.offer_id[:128], image_url=o.image[:600], description=o.description, category=o.category[:128], source="import",
                       listing_status="listed", listed_at=utcnow(), kind="affiliate", affiliate_url=o.url[:1500], merchant=(merchant or getattr(o, "merchant", "") or conn.name)[:80],
                       commission_pct=o.commission_pct))
        made += 1
    db.flush()
    return {"imported": made, "duplicate": dup, "no_rate": no_rate}


def refresh_from_feed(db: Session, conn: Connection, adapter) -> dict:
    """For a feed connection: new prices from the feed, and products that left the feed are hidden (we never send visitors to a dead link)."""
    try:
        feed = {o.offer_id: o for o in adapter.load()}
    except Exception as e:
        return {"error": str(e)[:200]}
    if not feed:
        return {"error": "feed khali"}
    changed = hidden = back = 0
    for p in db.query(Product).filter(Product.kind == "affiliate", Product.supplier_connection_id == conn.id).all():
        o = feed.get(p.supplier_vid)
        if o is None:
            if not p.auto_hidden:
                p.auto_hidden, p.hidden_reason = True, "Feed se hat gaya"
                hidden += 1
            continue
        if p.auto_hidden and p.hidden_reason == "Feed se hat gaya":
            p.auto_hidden, p.hidden_reason = False, ""
            back += 1
        try:
            usd = fx.to_usd(db, o.price, o.currency or (adapter.cfg.get("currency") or "USD"))
        except fx.FxError:
            continue
        if usd > 0 and usd != p.selling_price:
            p.selling_price = usd
            changed += 1
        p.last_synced_at = utcnow()
    db.flush()
    return {"repriced": changed, "hidden": hidden, "restored": back}


def run(db: Session, rt) -> dict:
    """The Affiliate agent: for every affiliate connection, add new offers (by keyword) until the target is reached and keep feed prices/links fresh."""
    cfg = config(db)
    if S.get_bool(db, "paused"):
        return {"paused": True}
    conns = db.query(Connection).filter(Connection.kind == "affiliate", Connection.enabled.is_(True)).order_by(Connection.id).all()
    if not conns:
        return {"none": True}
    if not cfg["on"]:
        return {"off": True}
    out = {"imported": 0, "repriced": 0, "hidden": 0, "errors": 0}
    for conn in conns:
        try:
            adapter = rt.connectors.build(conn)
            if hasattr(adapter, "load"):
                r = refresh_from_feed(db, conn, adapter)
                out["repriced"] += r.get("repriced", 0)
                out["hidden"] += r.get("hidden", 0)
            room = cfg["target"] - db.query(Product).filter(Product.kind == "affiliate", Product.listing_status == "listed").count()
            budget = min(room, cfg["batch"])
            for kw in (cfg["keywords"] or [""]):
                if budget <= 0:
                    break
                got = import_offers(db, conn, adapter.search(kw, 50), limit=budget, merchant=getattr(adapter, "merchant", ""))
                budget -= got["imported"]
                out["imported"] += got["imported"]
                if got["no_rate"]:
                    event(db, AGENT, f"{conn.name}: {got['no_rate']} offer ki currency ka rate nahi hai (Strategy page par rate daalo).", "warn")
            conn.last_status, conn.last_error = "ok", ""
        except Exception as e:  # one broken program must never stop the others
            out["errors"] += 1
            conn.last_status, conn.last_error = "error", str(e)[:300]
            event(db, AGENT, f"{conn.name}: {str(e)[:200]}", "warn")
        db.commit()
    return out


def import_now(db: Session, rt, conn: Connection, keywords: list[str], limit: int) -> dict:
    """The 'Abhi import karo' button: search these words (or the top of the feed) on one connection and add up to `limit` new products."""
    adapter = rt.connectors.build(conn)
    total = {"imported": 0, "duplicate": 0, "no_rate": 0}
    for kw in (keywords or [""]):
        if total["imported"] >= limit:
            break
        got = import_offers(db, conn, adapter.search(kw, 50), limit=limit - total["imported"], merchant=getattr(adapter, "merchant", ""))
        for k in total:
            total[k] += got[k]
    conn.last_status, conn.last_error = "ok", ""
    db.commit()
    return total


def go_url(db: Session, pid: int) -> str | None:
    """The merchant link for a click, and count the click. None when the product is not a live affiliate product."""
    p = db.get(Product, pid)
    if p is None or p.kind != "affiliate" or not p.active or p.auto_hidden or p.listing_status != "listed" or not p.affiliate_url.lower().startswith(("http://", "https://")):
        return None
    p.clicks = int(p.clicks or 0) + 1
    p.last_click_at = utcnow()
    db.commit()
    return p.affiliate_url
