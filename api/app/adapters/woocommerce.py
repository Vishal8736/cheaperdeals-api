"""WooCommerce adapter (REST v3 + webhooks).

Auth: HTTP Basic with consumer key/secret over HTTPS - confirmed in the WooCommerce REST docs.
Order update / order notes endpoints - confirmed in the same docs.
From WooCommerce knowledge, not fetched (marked VERIFY in docs/ASSUMPTIONS.md): webhook signature =
base64(HMAC-SHA256(raw_body, webhook_secret)) in header X-WC-Webhook-Signature; product `status`
(publish/draft) and `regular_price` fields; GET /orders?after=...&status=processing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from .base import NormalizedItem, NormalizedOrder, ProductPayload, StoreError, country_name

PAID_STATUSES = {"processing", "completed"}


def sign(secret: str, raw_body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()).decode()


class WooCommerceStore:
    name = "woocommerce"

    def __init__(self, base_url: str, key: str, secret: str, webhook_secret: str, client: httpx.Client | None = None):
        self.api = base_url.rstrip("/") + "/wp-json/wc/v3"
        self.webhook_secret = webhook_secret
        self._client = client or httpx.Client(auth=(key, secret), timeout=httpx.Timeout(20.0, connect=10.0))

    # ---- webhook ---------------------------------------------------------------------------
    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        if not self.webhook_secret or not signature:
            return False
        return hmac.compare_digest(sign(self.webhook_secret, raw_body), signature.strip())

    def normalize_order(self, p: dict) -> NormalizedOrder:
        ship = p.get("shipping") or {}
        bill = p.get("billing") or {}
        # Digital-only/virtual orders may have an empty shipping block; fall back to billing.
        src = ship if (ship.get("address_1") or ship.get("city")) else bill
        name = f"{src.get('first_name', '')} {src.get('last_name', '')}".strip() or \
               f"{bill.get('first_name', '')} {bill.get('last_name', '')}".strip()
        items = []
        for li in p.get("line_items", []):
            qty = int(li.get("quantity") or 1)
            line_total = Decimal(str(li.get("total") or "0"))
            unit = (line_total / qty) if qty else Decimal("0")
            pid = str(li.get("variation_id") or 0) if li.get("variation_id") else str(li.get("product_id", ""))
            items.append(NormalizedItem(
                store_product_id=pid, sku=str(li.get("sku") or ""), title=str(li.get("name") or ""),
                quantity=qty, unit_price=unit.quantize(Decimal("0.01")),
            ))
        status = str(p.get("status", ""))
        return NormalizedOrder(
            store_order_id=str(p["id"]), number=str(p.get("number") or p["id"]), status=status,
            paid=status in PAID_STATUSES, customer_name=name, email=str(bill.get("email") or ""),
            phone=str(src.get("phone") or bill.get("phone") or ""),
            address1=str(src.get("address_1") or ""), address2=str(src.get("address_2") or ""),
            city=str(src.get("city") or ""), state=str(src.get("state") or ""), zip_code=str(src.get("postcode") or ""),
            country_code=str(src.get("country") or ""), country=country_name(src.get("country") or ""),
            total=Decimal(str(p.get("total") or "0")), currency=str(p.get("currency") or ""), items=items,
        )

    # ---- calls -----------------------------------------------------------------------------
    def _call(self, method: str, path: str, **kw):
        try:
            r = self._client.request(method, self.api + path, **kw)
        except httpx.HTTPError as e:
            raise StoreError(f"WooCommerce unreachable: {e}")
        if r.status_code >= 400:
            raise StoreError(f"WooCommerce {method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def add_order_note(self, store_order_id: str, note: str, *, customer_note: bool = False) -> None:
        # customer_note=True makes WooCommerce email the customer - our zero-cost customer notification.
        self._call("POST", f"/orders/{store_order_id}/notes", json={"note": note, "customer_note": customer_note})

    def set_order_status(self, store_order_id: str, status: str) -> None:
        self._call("PUT", f"/orders/{store_order_id}", json={"status": status})

    def set_product_visible(self, store_product_id: str, visible: bool) -> None:
        self._call("PUT", f"/products/{store_product_id}", json={"status": "publish" if visible else "draft"})

    def set_product_price(self, store_product_id: str, price: Decimal) -> None:
        self._call("PUT", f"/products/{store_product_id}", json={"regular_price": f"{price:.2f}"})

    def list_recent_paid_orders(self, hours: int) -> list[dict]:
        after = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        out: list[dict] = []
        page = 1
        while page <= 10:
            rows = self._call("GET", "/orders", params={"after": after, "status": "processing", "per_page": 100, "page": page})
            out.extend(rows)
            if len(rows) < 100:
                break
            page += 1
        return out

    def publish_product(self, p: ProductPayload) -> tuple[str, str]:
        body = {"name": p.title, "type": "simple", "status": "publish", "regular_price": f"{p.price:.2f}", "sku": p.sku or "",
                "description": p.description, "manage_stock": True, "stock_quantity": max(0, int(p.stock))}
        if p.image_url.startswith("http"):
            body["images"] = [{"src": p.image_url}]
        d = self._call("POST", "/products", json=body)
        if not isinstance(d, dict) or "id" not in d:
            raise StoreError("WooCommerce did not return a product id")
        return str(d["id"]), str(d.get("permalink") or "")

    def test_connection(self) -> str:
        self._call("GET", "/products", params={"per_page": 1})
        return "WooCommerce answered and the keys work."
