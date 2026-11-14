"""POD publisher (v20): puts a design on product types (t-shirt, hoodie, mug, phone case, poster, tote, bottle, mouse pad, sticker) at a POD supplier
(Printful or Printify) through ITS API, then brings the finished product into our store.

Honest status: the request shapes below (catalog lookup, variant choice, product create, design upload) are written from memory of Printful's and
Printify's public API docs and were NOT run against a real account. Catalog titles differ per supplier, so every product type is looked up by words at run
time: a type the supplier does not have is reported as "not found", never faked. Nothing is sold at a guessed cost: the margin guard still runs before listing."""
from __future__ import annotations

import logging
import math
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from ..adapters.base import SupplierError
from ..models import Connection, Design, PodItem, Product
from . import settings_service as S
from .agents import event

log = logging.getLogger("cheapedeal.pod")

# words must ALL be in the catalog title (lower case); `avoid` words must not be; `colors` = preferred garment colours; `price` = default retail USD
PRODUCT_TYPES = {
    "tshirt": {"label": "T-shirt", "words": ["t-shirt"], "avoid": ["kids", "toddler", "baby", "youth", "tank", "long sleeve", "women", "crop", "embroider", "all-over", "all over"], "prefer": ["unisex", "bella"], "colors": ["black", "white"], "price": "24.99"},
    "hoodie": {"label": "Hoodie", "words": ["hoodie"], "avoid": ["kids", "youth", "zip", "all-over", "all over", "embroider"], "prefer": ["unisex", "heavy blend"], "colors": ["black", "navy"], "price": "44.99"},
    "mug": {"label": "Mug", "words": ["mug"], "avoid": ["travel", "camp", "enamel", "color", "colour", "two-tone", "all-over"], "prefer": ["white", "11"], "colors": [], "price": "16.99"},
    "phone_case": {"label": "Phone case (plastic)", "words": ["case", "iphone"], "avoid": ["wallet", "mag", "tough", "folio"], "prefer": ["clear", "snap"], "colors": [], "price": "22.99"},
    "poster": {"label": "Poster", "words": ["poster"], "avoid": ["framed", "canvas", "metal"], "prefer": ["matte", "premium"], "colors": [], "price": "19.99"},
    "tote": {"label": "Tote bag", "words": ["tote"], "avoid": ["all-over", "all over", "embroider"], "prefer": ["basic", "organic"], "colors": ["black", "natural", "white"], "price": "21.99"},
    "bottle": {"label": "Water bottle (plastic/steel)", "words": ["bottle"], "avoid": ["all-over", "dog", "pet"], "prefer": ["water"], "colors": [], "price": "24.99"},
    "mousepad": {"label": "Mouse pad", "words": ["mouse pad"], "avoid": ["gaming", "xl"], "prefer": ["rectangular"], "colors": [], "price": "17.99"},
    "sticker": {"label": "Sticker", "words": ["sticker"], "avoid": ["roll", "sheet", "magnet"], "prefer": ["kiss", "die"], "colors": [], "price": "4.99"},
}


def pick_title(items: list[dict], spec: dict, key=lambda x: x) -> Optional[dict]:
    """Best catalog entry for a spec: every word present, no avoid word, then most `prefer` words, then shortest title."""
    def t(x):
        return str(key(x)).lower()
    ok = [x for x in items if all(w in t(x) for w in spec["words"]) and not any(a in t(x) for a in spec["avoid"])]
    ok.sort(key=lambda x: (-sum(p in t(x) for p in spec["prefer"]), len(t(x))))
    return ok[0] if ok else None


def pick_variants(variants: list[dict], spec: dict, limit: int = 8, color_of=lambda v: str(v.get("color") or "")) -> list[dict]:
    """One colour (a preferred one if the product has colours) with all its sizes; products without colours: the first couple of variants."""
    if not variants:
        return []
    colours = [color_of(v).strip().lower() for v in variants]
    if not any(colours):
        return variants[:2]
    chosen = next((c for want in spec["colors"] for c in colours if c == want), next(c for c in colours if c))
    return [v for v, c in zip(variants, colours) if c == chosen][:limit]


def retail_price(cost: Decimal, markup: Decimal) -> Decimal:
    return Decimal(math.ceil(float(Decimal(cost) * markup))) - Decimal("0.01")


def title_of(design: Design, spec: dict) -> str:
    return f"{design.text.title()} - {spec['label'].split(' (')[0]}"


def design_url(cfg, design: Design) -> str:
    base = (cfg.public_url or "").rstrip("/")
    if not base.startswith("https://"):
        raise SupplierError("Printful design ko public link se leta hai: Render par chalao (Start). Local mein Printify use karo (wo file seedha upload leta hai).")
    return f"{base}/shop/design/{design.token}.png"


def publish_item(db: Session, rt, cfg, design: Design, conn: Connection, ptype: str) -> PodItem:
    """Create the product at the supplier and (if it can be sold) list it on our store. Never raises: the outcome is stored on the PodItem."""
    spec = PRODUCT_TYPES[ptype]
    item = db.query(PodItem).filter(PodItem.design_id == design.id, PodItem.connection_id == conn.id, PodItem.ptype == ptype).first()
    if item and item.status in ("created", "listed"):
        return item
    item = item or PodItem(design_id=design.id, connection_id=conn.id, ptype=ptype)
    item.title = title_of(design, spec)[:200]
    db.add(item)
    try:
        adapter = rt.supplier_for(conn.id)
        if not hasattr(adapter, "publish_design"):
            raise SupplierError("Ye supplier design push nahi karta.")
        kw = dict(spec=spec, title=item.title, description=f"{design.text}. Printed on demand, made just for you.", markup=_markup(db))
        if conn.type == "pod_printful":
            kw["design_url"] = design_url(cfg, design)
        else:
            kw["png"] = design.png
            kw["fixed_price"] = Decimal(S.get(db, f"pod_price_{ptype}", spec["price"]) or spec["price"])
        item.provider_ref = str(adapter.publish_design(**kw))[:80]
        item.status, item.error = "created", ""
        event(db, "pod", f"'{item.title}' {conn.name} par ban gaya.", "ok")
    except SupplierError as e:
        item.status, item.error = "error", str(e)[:400]
        event(db, "pod", f"'{item.title}' {conn.name} par nahi bana: {str(e)[:160]}", "warn")
    except Exception as e:
        item.status, item.error = "error", f"{type(e).__name__}: {e}"[:400]
        event(db, "pod", f"'{item.title}' error: {str(e)[:160]}", "error")
    db.flush()
    if item.status == "created":
        _bring_to_store(db, rt, conn, item)
    return item


def _markup(db: Session) -> Decimal:
    v = S.get(db, "pod_markup", "") or "2.2"
    try:
        return max(Decimal("1.2"), Decimal(v))
    except Exception:
        return Decimal("2.2")


def _bring_to_store(db: Session, rt, conn: Connection, item: PodItem) -> None:
    """Import what the supplier just created and list it (margin guard + real cost check included)."""
    from . import autocatalog, catalog
    try:
        adapter = rt.supplier_for(conn.id)
        items = [i for i in catalog.search(db, rt, conn.id, item.title, 1, 50) if item.title.lower()[:30] in i.title.lower() or item.provider_ref in i.supplier_product_id]
        res = catalog.import_items(db, rt, conn.id, {i.supplier_product_id for i in items}, item.title, 1, items=items)
        made = res["products"]
        if not made:
            item.error = "Supplier ne product bana diya; store mein abhi dikha nahi (agli baar import hoga)." if not res["skipped"] else ""
            if res["skipped"]:
                item.status = "listed"
            return
        listed = 0
        why = ""
        for p in made:
            ok, reason = autocatalog._activate(db, rt, adapter, p)
            listed += 1 if ok else 0
            why = why or reason
        item.status = "listed" if listed else "created"
        item.error = "" if listed else f"Store par nahi lagaya: {why}"[:400]
    except Exception as e:
        item.error = f"Store par lane mein dikkat: {str(e)[:300]}"
        log.warning("pod import failed: %s", e)
    db.flush()


def counts(db: Session) -> dict:
    return {"designs": db.query(Design).count(), "created": db.query(PodItem).filter(PodItem.status.in_(("created", "listed"))).count(),
            "listed": db.query(PodItem).filter(PodItem.status == "listed").count(), "errors": db.query(PodItem).filter(PodItem.status == "error").count(),
            "products": db.query(Product).filter(Product.listing_status == "listed").count()}
