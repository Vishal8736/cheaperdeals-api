"""The business switchboard (v20): one place to see and switch the businesses that run side by side. Each business has its own ON/OFF,
its own agents and (optionally) its own website. Switching a business off puts only ITS agents to sleep; the others keep going."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Connection, Design, Order, OrderItem, Product
from . import mode as MODE
from . import settings_service as S


@dataclass(frozen=True)
class Biz:
    key: str
    label: str
    blurb: str
    page: str
    api: str
    built: bool = True


BUSINESSES = [
    Biz("affiliate", "Affiliate marketing", "Partner products dikhte hain, 'Buy at' se merchant par bhejte hain, kamai network ki API se aati hai.", "/programs", "Awin, impact, CJ, Amazon, eBay ... ki API"),
    Biz("pod", "Print-on-demand", "Designs yahan bante hain, Printful/Printify par product banta hai, store par lagta hai; print aur shipping wo karte hain.", "/pod", "Printful / Printify API"),
    Biz("dropship", "Dropshipping", "Supplier (CJ) se product, customer ka order supplier ko jata hai.", "/catalog", "CJ API (browser agent v22 mein)"),
    Biz("ebook", "Ebook banana + bechna", "AI se ebook likhna aur apni website par bechna.", "/business", "payment API", built=False),
]
BY_KEY = {b.key: b for b in BUSINESSES}


def pod_connection_ids(db: Session) -> list[int]:
    return [c.id for c in db.query(Connection).filter(Connection.type.like("pod\\_%", escape="\\")).all()]


def product_kinds(db: Session, biz: str):
    """SQL condition: products that belong to this business (affiliate by kind, pod = from a POD supplier connection, dropship = the rest)."""
    pod_ids = pod_connection_ids(db) or [-1]
    if biz == "affiliate":
        return Product.kind == "affiliate"
    if biz == "pod":
        return (Product.kind != "affiliate") & Product.supplier_connection_id.in_(pod_ids)
    return (Product.kind != "affiliate") & or_(Product.supplier_connection_id.is_(None), Product.supplier_connection_id.notin_(pod_ids))


def filter_products(db: Session, q, biz: str = ""):
    """Keep only products of businesses that are ON; `biz` narrows a dedicated website to one business."""
    keys = [b for b in ("affiliate", "pod", "dropship") if MODE.is_on(db, b) and (not biz or biz == b)]
    if not keys:
        return q.filter(Product.id < 0)
    return q.filter(or_(*[product_kinds(db, k) for k in keys]))


def site_of(db: Session, key: str) -> str:
    return S.get(db, f"biz_{key}_site", "").strip()


def set_site(db: Session, key: str, url: str) -> Optional[str]:
    url = url.strip().rstrip("/")
    if url and not url.lower().startswith(("http://", "https://")):
        return "Website URL http:// ya https:// se shuru hona chahiye."
    S.set_value(db, f"biz_{key}_site", url)
    return None


def status(db: Session) -> list[dict]:
    now = utcnow()
    out = []
    for b in BUSINESSES:
        row = {"key": b.key, "label": b.label, "blurb": b.blurb, "page": b.page, "api": b.api, "built": b.built, "on": MODE.is_on(db, b.key), "site": site_of(db, b.key), "stats": []}
        if b.key == "affiliate":
            from . import earnings as EA
            c = EA.click_stats(db)
            row["stats"] = [("Live products", db.query(Product).filter(Product.kind == "affiliate", Product.listing_status == "listed").count()),
                            ("Clicks (hafta)", c["week"]), ("Kamai 30 din", f"{EA.totals(db, 30)['total']:.2f} USD")]
        elif b.key == "pod":
            row["stats"] = [("Designs", db.query(Design).count()),
                            ("Live products", db.query(Product).filter(product_kinds(db, "pod"), Product.listing_status == "listed").count()),
                            ("Orders (30 din)", db.query(OrderItem.order_id).join(Order, Order.id == OrderItem.order_id).join(Product, Product.id == OrderItem.product_id)
                             .filter(product_kinds(db, "pod"), Order.created_at >= now - timedelta(days=30)).distinct().count())]
        elif b.key == "dropship":
            row["stats"] = [("Live products", db.query(Product).filter(product_kinds(db, "dropship"), Product.listing_status == "listed").count())]
        out.append(row)
    return out


def start_selected(db: Session, keys: list[str]) -> list[str]:
    return [k for k in keys if MODE.set_on(db, k, True)]


def stop_selected(db: Session, keys: list[str]) -> list[str]:
    return [k for k in keys if MODE.set_on(db, k, False)]
