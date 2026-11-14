"""The Catalog agent that works by itself: after Start it browses the supplier (CJ) by keyword, imports new products, reads their real stock and
price, and puts the good ones on the website. Nothing is invented: a product goes live only when the supplier shows stock and the margin
is above the minimum. Without this agent products had to be picked by hand on the Catalog page, so a freshly hosted store stayed empty."""
from __future__ import annotations

import time
from decimal import Decimal

from sqlalchemy.orm import Session

from ..adapters.base import StoreError, SupplierError
from ..adapters.mock_supplier import MockSupplier
from ..db import utcnow
from ..models import Product
from . import catalog as C
from . import playbook as PB
from . import settings_service as S
from .agents import event
from .pricing import sell_price

SLEEP = time.sleep  # tests replace this (CJ allows about one call per second)
DEFAULT_KEYWORDS = "phone case, led light, smart watch, earbuds, kitchen gadget, pet supplies, car accessories, fitness, backpack, jewelry, home decor, toys"
PAGE = 50


def config(db: Session) -> dict:
    def num(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(float(S.get(db, key, str(default)) or default))))
        except ValueError:
            return default
    kws = [k.strip() for k in (S.get(db, "auto_catalog_keywords", "") or DEFAULT_KEYWORDS).replace("\n", ",").split(",") if k.strip()]
    return {"on": S.get(db, "auto_catalog", "true") == "true", "keywords": kws or [k.strip() for k in DEFAULT_KEYWORDS.split(",")],
            "target": num("auto_catalog_target", 30, 1, 500), "batch": num("auto_catalog_batch", 6, 1, 25)}


def counts(db: Session) -> dict:
    q = db.query(Product).filter(Product.source == "import", Product.kind != "affiliate")
    return {"imported": q.count(), "listed": q.filter(Product.listing_status == "listed").count()}


def _activate(db: Session, rt, adapter, p: Product) -> tuple[bool, str]:
    """Read the supplier's real stock/price for a fresh draft and put it on the website if it is sellable. Returns (listed, why not)."""
    from . import shop
    from .orders import effective_min_margin
    try:
        info = adapter.get_product_info(p.supplier_vid)
    except SupplierError as e:
        return False, f"supplier data nahi mila ({str(e)[:80]})"
    p.stock, p.last_synced_at = int(info.stock), utcnow()
    if Decimal(info.price) > 0:
        p.supplier_price = Decimal(info.price)
        markup = S.get_decimal(db, "markup_pct")
        p.selling_price = sell_price(p.supplier_price, PB.markup_for(db, p.supplier_price, markup), S.get(db, "price_ending"))
    if p.stock <= 0:
        return False, "supplier ke paas stock nahi"
    if p.margin_pct() < effective_min_margin(db, p):
        return False, f"margin {p.margin_pct()}% minimum se kam"
    try:
        ok = shop.publish_to_website(db, p)
    except StoreError as e:
        return False, str(e)[:80]
    if ok:
        event(db, "lister", f"'{p.title[:80]}' website par laga diya (stock {p.stock}, price {p.selling_price})", "ok")
    return ok, "" if ok else "selling price 0 hai"


def run(db: Session, rt) -> dict:
    if S.get_bool(db, "paused"):
        return {"paused": True}
    cfg = config(db)
    if not cfg["on"]:
        return {"off": True}
    have = counts(db)
    if have["listed"] >= cfg["target"]:
        return {"full": True, **have, "target": cfg["target"]}
    try:
        adapter = C.supplier_adapter(rt, 0)
        if isinstance(adapter, MockSupplier):  # no real supplier connected: never put the built-in DEMO products on a live website
            event(db, "catalog", "CJ API key set nahi hai, isliye auto catalog ruka hua hai (demo products website par nahi lagaunga). API Keys page par CJ key daalo.", "warn")
            return {"error": "CJ API key set nahi hai."}
        if getattr(adapter, "list_catalog", None) is None:
            return {"error": "Ye supplier apna catalog nahi dikha sakta."}
    except Exception as e:  # no supplier configured
        event(db, "catalog", f"Auto catalog supplier tak nahi pahunch paya: {str(e)[:160]}", "warn")
        return {"error": str(e)[:200]}
    kws = cfg["keywords"]
    try:
        ki, page = (int(x) for x in S.get(db, "auto_catalog_cursor", "0:1").split(":"))
    except ValueError:
        ki, page = 0, 1
    res = {"imported": 0, "listed": 0, "drafts": 0, "skipped_reasons": [], "target": cfg["target"]}
    empty_in_a_row = 0
    searches = 0
    try:
        while res["imported"] < cfg["batch"] and have["listed"] + res["listed"] < cfg["target"] and empty_in_a_row < len(kws) * 2 and searches < 40:
            kw = kws[ki % len(kws)]
            items = C.search(db, rt, 0, kw, page, PAGE)
            known = {r[0] for r in db.query(Product.supplier_product_id).filter(Product.supplier_product_id != "").all()}
            fresh = [i for i in items if i.supplier_product_id and i.supplier_product_id not in known][: cfg["batch"] - res["imported"]]
            used_page = page
            if len(items) < PAGE:  # that keyword is used up: next keyword from page 1
                ki, page = ki + 1, 1
            else:
                page += 1
            searches += 1
            if not fresh:  # nothing new on this page
                empty_in_a_row += 1
                continue
            empty_in_a_row = 0
            made = C.import_items(db, rt, 0, {i.supplier_product_id for i in fresh}, kw, used_page, items=fresh)
            res["imported"] += made["imported"]
            for p in made["products"]:
                SLEEP(0.2)
                ok, why = _activate(db, rt, adapter, p)
                res["listed" if ok else "drafts"] += 1
                if not ok:
                    res["skipped_reasons"].append(f"{p.title[:40]}: {why}")
            res["failed"] = made["failed"][:3]
            db.commit()
            S.set_value(db, "auto_catalog_cursor", f"{ki}:{page}")
            db.commit()
    except SupplierError as e:
        db.commit()
        S.set_value(db, "auto_catalog_cursor", f"{ki}:{page}")
        db.commit()
        res["error"] = str(e)[:200]
        event(db, "catalog", f"Auto catalog ruk gaya: {res['error']}", "warn")
        db.commit()
        rt.alert(db, level="warning", source="catalog", message=f"Auto catalog ruk gaya: {res['error']}", dedupe_key="autocat-err", dedupe_hours=6)
    S.set_value(db, "auto_catalog_cursor", f"{ki}:{page}")
    S.mark_run(db, "auto_catalog", f"{res['imported']} imported, {res['listed']} listed")
    db.commit()
    return res
