"""Print-on-demand suppliers: you design products inside the POD company's own dashboard, we import them, sell them on your website, and when a
customer pays we send the order to the POD company, which prints and ships it (same flow as a dropship supplier).

Printful: endpoints and order fields from Printful's developer docs (store/products, orders, @external_id). The base-cost lookup
(/products/variant/{id}) is from memory of their catalog API. Printify: shops/products/orders paths and the order body from Printify's OpenAPI file.
None of this was run against a real account."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Optional

from ..adapters.base import (
    CatalogItem, SupplierError, SupplierOrderInfo, SupplierOrderRequest, SupplierOrderResult, SupplierProductInfo,
)
from . import affiliate_sources as AS
from .feedparse import parse_price


def _call(method: str, url: str, headers: dict, body: Optional[dict] = None, *, what: str) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers = {**headers, "Content-Type": "application/json"}
    st, raw = AS.request(method, url, headers, data)
    if st in (401, 403):
        raise SupplierError(f"{what}: token galat ya permission nahi (HTTP {st}).", status_code=st)
    if st == 429 or st >= 500:
        raise SupplierError(f"{what}: abhi busy (HTTP {st}), baad mein try hoga.", retryable=True, status_code=st)
    if st >= 400:
        raise SupplierError(f"{what}: HTTP {st} {raw[:200].decode('utf-8', 'replace')}", status_code=st)
    try:
        return json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        raise SupplierError(f"{what}: jawab samajh nahi aaya.")


def _split_name(name: str) -> tuple[str, str]:
    parts = (name or "").strip().split(None, 1)
    return (parts[0] if parts else "-", parts[1] if len(parts) > 1 else "-")


class PrintfulSupplier:
    name = "printful"
    supports_lookup = True
    BASE = "https://api.printful.com"

    def __init__(self, cfg: dict, sec: dict):
        self.h = {"Authorization": f"Bearer {sec.get('api_token', '')}", "User-Agent": "CheapeDeal/18"}
        if cfg.get("store_id"):
            self.h["X-PF-Store-Id"] = str(cfg["store_id"]).strip()

    def _get(self, path: str, what: str) -> dict:
        return _call("GET", self.BASE + path, self.h, what=what)

    def test_connection(self) -> str:
        d = self._get("/store/products?limit=1", "Printful")
        total = (d.get("paging") or {}).get("total", len(d.get("result") or []))
        return f"Printful se connect ho gaya. Tumhare store mein {total} product hain."

    def list_catalog(self, query: str = "", page: int = 1, page_size: int = 20) -> list[CatalogItem]:
        d = self._get(f"/store/products?offset={max(0, (page - 1) * page_size)}&limit={min(100, page_size)}" + (f"&search={query}" if query else ""), "Printful products")
        out = []
        for sp in d.get("result") or []:
            det = self._get(f"/store/products/{sp['id']}", "Printful product").get("result") or {}
            for v in det.get("sync_variants") or []:
                if not v.get("id"):
                    continue
                cost = self.base_cost(v)
                out.append(CatalogItem(supplier_product_id=f"{sp['id']}:{v['id']}", vid=str(v["id"]), title=f"{sp.get('name', '')} - {v.get('name', '')}"[:250],
                                       cost=cost, stock=999 if v.get("availability_status", "active") in ("active", "") else 0, sku=str(v.get("sku") or ""),
                                       image_url=str(sp.get("thumbnail_url") or ""), description="", category="Print on demand"))
        return out

    def base_cost(self, sync_variant: dict) -> Decimal:
        """What Printful charges US for this variant (the base price), never the retail price you set."""
        vid = (sync_variant.get("product") or {}).get("variant_id") or sync_variant.get("variant_id")
        if not vid:
            raise SupplierError("Printful: is variant ka catalog id nahi mila, cost padh nahi sakte.")
        d = self._get(f"/products/variant/{vid}", "Printful cost").get("result") or {}
        price = parse_price((d.get("variant") or {}).get("price"))
        if price is None:
            raise SupplierError("Printful: base cost nahi mila (koi andaaza nahi lagaya).")
        return price

    def get_product_info(self, vid: str) -> SupplierProductInfo:
        sv = (self._get(f"/store/variants/{vid}", "Printful variant").get("result") or {}).get("sync_variant") or {}
        cost = self.base_cost(sv)
        return SupplierProductInfo(vid=str(vid), stock=999, price=cost)

    def create_order(self, req: SupplierOrderRequest) -> SupplierOrderResult:
        body = {"external_id": req.order_number, "shipping": "STANDARD",
                "recipient": {"name": req.customer_name, "address1": req.address1, "address2": req.address2, "city": req.city, "state_code": req.province,
                              "country_code": req.country_code, "zip": req.zip_code, "phone": req.phone, "email": req.email},
                "items": [{"sync_variant_id": int(ln.vid), "quantity": ln.quantity} for ln in req.lines]}
        d = _call("POST", self.BASE + "/orders?confirm=true", self.h, body, what="Printful order")
        r = d.get("result") or {}
        if not r.get("id"):
            raise SupplierError("Printful ne order ka id nahi diya.", ambiguous=True)
        return SupplierOrderResult(supplier_order_id=str(r["id"]), status=str(r.get("status", "")), raw=r)

    def find_order(self, order_number: str) -> Optional[SupplierOrderResult]:
        try:
            r = self._get(f"/orders/@{order_number}", "Printful order").get("result") or {}
        except SupplierError as e:
            if e.status_code == 404:
                return None
            raise
        return SupplierOrderResult(supplier_order_id=str(r["id"]), status=str(r.get("status", "")), raw=r) if r.get("id") else None

    def get_order_info(self, supplier_order_id: str) -> SupplierOrderInfo:
        r = self._get(f"/orders/{supplier_order_id}", "Printful order").get("result") or {}
        raw = str(r.get("status", ""))
        status = {"draft": "pending", "pending": "pending", "inprocess": "confirmed", "onhold": "confirmed", "partial": "confirmed", "fulfilled": "shipped",
                  "canceled": "cancelled", "failed": "cancelled"}.get(raw, "pending")
        sh = (r.get("shipments") or [{}])[0]
        return SupplierOrderInfo(supplier_order_id=str(supplier_order_id), status=status, raw_status=raw, tracking_number=str(sh.get("tracking_number") or ""),
                                 carrier=str(sh.get("carrier") or sh.get("service") or ""), raw=r)


class PrintifySupplier:
    name = "printify"
    supports_lookup = False
    BASE = "https://api.printify.com/v1"

    def __init__(self, cfg: dict, sec: dict):
        self.h = {"Authorization": f"Bearer {sec.get('api_token', '')}", "User-Agent": "CheapeDeal/18"}  # Printify requires a User-Agent
        self.shop = str(cfg.get("shop_id") or "").strip()

    def _shop(self) -> str:
        if self.shop:
            return self.shop
        shops = _call("GET", self.BASE + "/shops.json", self.h, what="Printify shops")
        shops = shops if isinstance(shops, list) else shops.get("data", [])
        if len(shops) != 1:
            raise SupplierError(f"Printify account mein {len(shops)} shops hain: connection mein Shop ID daalo.")
        self.shop = str(shops[0]["id"])
        return self.shop

    def test_connection(self) -> str:
        d = _call("GET", self.BASE + f"/shops/{self._shop()}/products.json?limit=1", self.h, what="Printify")
        return f"Printify se connect ho gaya (shop {self.shop}), {d.get('total', len(d.get('data') or []))} product hain."

    def list_catalog(self, query: str = "", page: int = 1, page_size: int = 20) -> list[CatalogItem]:
        d = _call("GET", self.BASE + f"/shops/{self._shop()}/products.json?page={max(1, page)}&limit={min(50, page_size)}", self.h, what="Printify products")
        out = []
        q = (query or "").lower()
        for p in d.get("data") or []:
            if q and q not in str(p.get("title", "")).lower():
                continue
            img = next((i.get("src") for i in p.get("images") or [] if i.get("is_default")), "") or ((p.get("images") or [{}])[0].get("src", ""))
            for v in p.get("variants") or []:
                if not v.get("is_enabled", True) or v.get("cost") is None:
                    continue
                out.append(CatalogItem(supplier_product_id=f"{p['id']}:{v['id']}", vid=f"{p['id']}:{v['id']}", title=f"{p.get('title', '')} - {v.get('title', '')}"[:250],
                                       cost=(Decimal(str(v["cost"])) / 100).quantize(Decimal("0.01")), stock=999 if v.get("is_available", True) else 0,
                                       sku=str(v.get("sku") or ""), image_url=str(img or ""), description=str(p.get("description") or "")[:600], category="Print on demand"))
        return out

    def get_product_info(self, vid: str) -> SupplierProductInfo:
        pid, _, vnum = str(vid).partition(":")
        p = _call("GET", self.BASE + f"/shops/{self._shop()}/products/{pid}.json", self.h, what="Printify product")
        for v in p.get("variants") or []:
            if str(v.get("id")) == vnum and v.get("cost") is not None:
                return SupplierProductInfo(vid=vid, stock=999 if v.get("is_available", True) and v.get("is_enabled", True) else 0, price=(Decimal(str(v["cost"])) / 100).quantize(Decimal("0.01")))
        raise SupplierError("Printify: ye variant ab nahi mila.")

    def create_order(self, req: SupplierOrderRequest) -> SupplierOrderResult:
        first, last = _split_name(req.customer_name)
        items = []
        for ln in req.lines:
            pid, _, vnum = str(ln.vid).partition(":")
            if not vnum.isdigit():
                raise SupplierError(f"Printify: variant id '{ln.vid}' galat hai (product:variant chahiye).")
            items.append({"product_id": pid, "variant_id": int(vnum), "quantity": ln.quantity})
        body = {"external_id": req.order_number, "label": req.order_number, "line_items": items, "shipping_method": 1, "send_shipping_notification": False,
                "address_to": {"first_name": first, "last_name": last, "email": req.email, "phone": req.phone, "country": req.country_code, "region": req.province,
                               "address1": req.address1, "address2": req.address2, "city": req.city, "zip": req.zip_code}}
        d = _call("POST", self.BASE + f"/shops/{self._shop()}/orders.json", self.h, body, what="Printify order")
        if not d.get("id"):
            raise SupplierError("Printify ne order ka id nahi diya.", ambiguous=True)
        try:  # an order made through the API still has to be sent to production
            _call("POST", self.BASE + f"/shops/{self._shop()}/orders/{d['id']}/send_to_production.json", self.h, {}, what="Printify production")
        except SupplierError as e:
            if e.retryable:
                raise SupplierError(f"Printify order {d['id']} ban gaya par production par nahi gaya: {e}", ambiguous=True)
        return SupplierOrderResult(supplier_order_id=str(d["id"]), status=str(d.get("status", "")), raw=d)

    def find_order(self, order_number: str) -> Optional[SupplierOrderResult]:
        return None

    def get_order_info(self, supplier_order_id: str) -> SupplierOrderInfo:
        d = _call("GET", self.BASE + f"/shops/{self._shop()}/orders/{supplier_order_id}.json", self.h, what="Printify order")
        raw = str(d.get("status", ""))
        status = {"pending": "pending", "on-hold": "pending", "sending-to-production": "confirmed", "in-production": "confirmed", "partially-fulfilled": "confirmed",
                  "fulfilled": "shipped", "canceled": "cancelled", "has-issues": "pending"}.get(raw, "pending")
        sh = (d.get("shipments") or [{}])[0]
        if sh.get("delivered_at"):
            status = "delivered"
        return SupplierOrderInfo(supplier_order_id=str(supplier_order_id), status=status, raw_status=raw, tracking_number=str(sh.get("number") or ""),
                                 carrier=str(sh.get("carrier") or ""), raw=d)
