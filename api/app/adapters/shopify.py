"""Shopify store adapter (Admin REST API + webhooks).

Auth: header X-Shopify-Access-Token (a custom-app Admin API token). Webhook check: base64(HMAC-SHA256(client secret, raw body))
in header X-Shopify-Hmac-Sha256. Both are standard Shopify behaviour; field names below follow the REST Admin API
(products.json, variants/{id}.json, orders.json). Marked VERIFY in docs/ASSUMPTIONS.md until tried on a real shop.
Stock levels are not pushed (inventory needs the separate Inventory API); the stock guard hides a product (status=draft) instead.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx

from .base import NormalizedItem, NormalizedOrder, ProductPayload, StoreError, country_name

PAID = {"paid", "partially_paid"}


def _d(v) -> Decimal:
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return Decimal("0")


class ShopifyStore:
    name = "shopify"

    def __init__(self, shop: str, token: str, webhook_secret: str = "", api_version: str = "2024-10", client: httpx.Client | None = None):
        shop = shop.strip().replace("https://", "").replace("http://", "").strip("/")
        if not shop:
            raise ValueError("Shop address is required, like myshop.myshopify.com")
        self.shop, self.version, self.webhook_secret = shop, api_version, webhook_secret
        self.api = f"https://{shop}/admin/api/{api_version}"
        self._client = client or httpx.Client(headers={"X-Shopify-Access-Token": token}, timeout=httpx.Timeout(20.0, connect=10.0))

    def _call(self, method: str, path: str, **kw):
        try:
            r = self._client.request(method, self.api + path, **kw)
        except httpx.HTTPError as e:
            raise StoreError(f"Shopify unreachable: {e}")
        if r.status_code >= 400:
            raise StoreError(f"Shopify {method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    # ---- webhook ---------------------------------------------------------------------------
    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        if not self.webhook_secret or not signature:
            return False
        want = base64.b64encode(hmac.new(self.webhook_secret.encode(), raw_body, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(want, signature.strip())

    def normalize_order(self, p: dict) -> NormalizedOrder:
        ship = p.get("shipping_address") or p.get("billing_address") or {}
        cc = str(ship.get("country_code") or "")
        status = str(p.get("financial_status") or "")
        items = [NormalizedItem(store_product_id=str(li.get("product_id") or ""), sku=str(li.get("sku") or ""), title=str(li.get("title") or ""),
                                quantity=int(li.get("quantity") or 1), unit_price=_d(li.get("price")))
                 for li in p.get("line_items", [])]
        return NormalizedOrder(
            store_order_id=str(p["id"]), number=str(p.get("order_number") or p.get("name") or p["id"]), status=status, paid=status in PAID,
            customer_name=str(ship.get("name") or "").strip(), email=str(p.get("email") or ""), phone=str(ship.get("phone") or p.get("phone") or ""),
            address1=str(ship.get("address1") or ""), address2=str(ship.get("address2") or ""), city=str(ship.get("city") or ""),
            state=str(ship.get("province_code") or ship.get("province") or ""), zip_code=str(ship.get("zip") or ""), country_code=cc,
            country=str(ship.get("country") or country_name(cc)), total=_d(p.get("total_price")), currency=str(p.get("currency") or ""), items=items)

    # ---- orders ----------------------------------------------------------------------------
    def add_order_note(self, store_order_id: str, note: str, *, customer_note: bool = False) -> None:
        cur = self._call("GET", f"/orders/{store_order_id}.json", params={"fields": "id,note"}).get("order", {})
        merged = (str(cur.get("note") or "") + "\n" + note).strip()
        self._call("PUT", f"/orders/{store_order_id}.json", json={"order": {"id": int(store_order_id), "note": merged}})

    def set_order_status(self, store_order_id: str, status: str) -> None:
        if status == "completed":
            self._call("PUT", f"/orders/{store_order_id}.json", json={"order": {"id": int(store_order_id), "tags": "cheapedeal-delivered"}})

    def list_recent_paid_orders(self, hours: int) -> list[dict]:
        after = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._call("GET", "/orders.json", params={"status": "any", "financial_status": "paid", "created_at_min": after, "limit": 250}).get("orders", [])

    # ---- products --------------------------------------------------------------------------
    def publish_product(self, p: ProductPayload) -> tuple[str, str]:
        prod = {"title": p.title, "body_html": p.description or "", "product_type": p.category, "status": "active",
                "variants": [{"price": f"{p.price:.2f}", "sku": p.sku or None}]}
        if p.image_url.startswith("http"):
            prod["images"] = [{"src": p.image_url}]
        d = self._call("POST", "/products.json", json={"product": prod}).get("product") or {}
        if not d.get("id"):
            raise StoreError("Shopify did not return a product id")
        return str(d["id"]), f"https://{self.shop}/products/{d.get('handle', '')}"

    def set_product_visible(self, store_product_id: str, visible: bool) -> None:
        self._call("PUT", f"/products/{store_product_id}.json", json={"product": {"id": int(store_product_id), "status": "active" if visible else "draft"}})

    def set_product_price(self, store_product_id: str, price: Decimal) -> None:
        variants = self._call("GET", f"/products/{store_product_id}.json", params={"fields": "id,variants"}).get("product", {}).get("variants", [])
        if not variants:
            raise StoreError("Shopify product has no variant to reprice")
        self._call("PUT", f"/variants/{variants[0]['id']}.json", json={"variant": {"id": variants[0]["id"], "price": f"{price:.2f}"}})

    def test_connection(self) -> str:
        shop = self._call("GET", "/shop.json").get("shop", {})
        return f"Shopify answered: {shop.get('name', self.shop)}."
