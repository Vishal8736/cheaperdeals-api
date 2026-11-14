"""Catalog: browse a supplier, import products, publish them on stores/websites."""
from __future__ import annotations

import secrets
from decimal import Decimal

from sqlalchemy.orm import Session

from ..adapters.base import CatalogItem, ProductPayload, StoreError, SupplierError
from ..db import utcnow
from ..models import Connection, Product, ProductListing
from . import settings_service as S
from .agents import event
from .outbound import emit
from .pricing import sell_price
from . import playbook as PB


def supplier_adapter(rt, connection_id: int):
    """0 = the built-in supplier from .env."""
    return rt.supplier_for(connection_id) if connection_id else (rt.connectors.supplier(None, rt.supplier) if rt.connectors else rt.supplier)


def search(db: Session, rt, connection_id: int, query: str = "", page: int = 1, page_size: int = 20) -> list[CatalogItem]:
    adapter = supplier_adapter(rt, connection_id)
    fn = getattr(adapter, "list_catalog", None)
    if fn is None:
        raise SupplierError("This supplier cannot list its products. You can still add products by hand.")
    try:
        return list(fn(query, page, page_size))
    except NotImplementedError as e:
        raise SupplierError(str(e))


def already_imported(db: Session, connection_id: int, items: list[CatalogItem]) -> set[str]:
    cid = connection_id or None
    vids = [i.vid for i in items if i.vid]
    if not vids:
        return set()
    q = db.query(Product.supplier_vid).filter(Product.supplier_vid.in_(vids), Product.supplier_connection_id.is_(None) if cid is None
                                              else Product.supplier_connection_id == cid)
    return {r[0] for r in q.all()}


def import_items(db: Session, rt, connection_id: int, wanted: set[str], query: str, page: int, *, markup_pct: Decimal | None = None, items: list | None = None) -> dict:
    """Import the chosen products (matched by supplier product id) as drafts. Prices are re-read from the supplier, never from the browser."""
    items = [i for i in (items if items is not None else search(db, rt, connection_id, query, page, 50)) if i.supplier_product_id in wanted]
    adapter = supplier_adapter(rt, connection_id)
    markup = markup_pct if markup_pct is not None else S.get_decimal(db, "markup_pct")
    cid = connection_id or None
    made, skipped, failed = [], 0, []
    for it in items:
        vid, cost = it.vid, it.cost
        if not vid and hasattr(adapter, "variants_of"):  # e.g. CJ lists products; ordering needs a variant id
            try:
                vs = adapter.variants_of(it.supplier_product_id)
                if vs:
                    vid = str(vs[0].get("vid") or "")
                    cost = Decimal(str(vs[0].get("variantSellPrice") or cost)) if vs[0].get("variantSellPrice") else cost
            except Exception as e:
                failed.append(f"{it.title}: could not read variants ({e})")
                continue
        if not vid:
            failed.append(f"{it.title}: supplier gave no variant id")
            continue
        exists = db.query(Product).filter(Product.supplier_vid == vid,
                                          Product.supplier_connection_id.is_(None) if cid is None else Product.supplier_connection_id == cid).first()
        if exists:
            skipped += 1
            continue
        sell = sell_price(cost, PB.markup_for(db, cost, markup) if markup_pct is None else markup, S.get(db, "price_ending")) if cost > 0 else Decimal("0")
        p = Product(store_product_id=f"draft-{secrets.token_hex(5)}", sku=it.sku, title=it.title[:255], supplier_vid=vid,
                    supplier_price=cost, selling_price=sell, stock=it.stock, supplier_connection_id=cid,
                    supplier_product_id=it.supplier_product_id, image_url=it.image_url[:600], description=it.description,
                    category=it.category[:128], source="import", listing_status="draft", last_synced_at=utcnow())
        db.add(p)
        made.append(p)
    db.flush()
    if made or skipped or failed:
        event(db, "catalog", f"Imported {len(made)} product(s)" + (f", {skipped} already existed" if skipped else "")
              + (f", {len(failed)} failed" if failed else ""), "ok" if made else "warn")
    return {"imported": len(made), "skipped": skipped, "failed": failed, "products": made}


def _payload(p: Product, price: Decimal | None = None) -> ProductPayload:
    return ProductPayload(title=p.title, price=Decimal(price if price is not None else p.selling_price), sku=p.sku, description=p.description, image_url=p.image_url,
                          stock=p.stock, category=p.category, internal_id=str(p.id))


def list_on_store(db: Session, rt, p: Product, store_connection_id: int, price: Decimal | None = None) -> tuple[bool, str]:
    """Publish one product on a store (0 = built-in store). price = this store's own price (multi-country); None = the product price.
    Returns (ok, message). Never raises."""
    label = "your store"
    try:
        if store_connection_id:
            row = db.get(Connection, store_connection_id)
            label = row.name if row else label
        adapter = rt.store_for(store_connection_id) if store_connection_id else rt.store
        pub = getattr(adapter, "publish_product", None)
        if pub is None:
            raise StoreError("This store cannot publish products")
        existing = db.query(ProductListing).filter(ProductListing.product_id == p.id, ProductListing.connection_id == store_connection_id).first()
        if existing and existing.status == "listed":
            if price is not None and (existing.price_usd is None or Decimal(existing.price_usd) != Decimal(price)) and existing.remote_id:
                try:
                    adapter.set_product_price(existing.remote_id, Decimal(price))  # store first; retried next run if it fails
                except StoreError as e:
                    return False, f"could not change the price on {label}: {e}"
                existing.price_usd = Decimal(price)
                db.flush()
            return True, f"already listed on {label}"
        if Decimal(p.selling_price) <= 0:
            raise StoreError("Set a selling price above 0 first")
        remote_id, url = pub(_payload(p, price))
        lst = existing or ProductListing(product_id=p.id, connection_id=store_connection_id)
        lst.store_label, lst.remote_id, lst.url, lst.status, lst.error = label, remote_id, url, "listed", ""
        lst.price_usd = Decimal(price) if price is not None else None
        if existing is None:
            db.add(lst)
        if store_connection_id == 0 and p.store_product_id.startswith("draft-"):
            p.store_product_id = remote_id  # the built-in store's id is the id orders will carry
        p.listing_status, p.listed_at = "listed", utcnow()
        db.flush()
        event(db, "lister", f"Listed '{p.title}' on {label}", "ok")
        emit(db, "product.listed", {"product_id": p.id, "title": p.title, "store": label, "remote_id": remote_id, "url": url})
        return True, f"listed on {label}"
    except Exception as e:  # one failing product must not stop the batch
        lst = db.query(ProductListing).filter(ProductListing.product_id == p.id, ProductListing.connection_id == store_connection_id).first()
        if lst is None:
            lst = ProductListing(product_id=p.id, connection_id=store_connection_id)
            db.add(lst)
        lst.store_label, lst.status, lst.error = label, "error", str(e)[:400]
        db.flush()
        event(db, "lister", f"Could not list '{p.title}' on {label}: {e}", "error")
        return False, str(e)
