"""Custom website store: list products on YOUR OWN website, and receive its orders, over plain HTTP + JSON.

Your site needs only:
  - an endpoint that accepts a new product (POST)            -> we send it when you press "List on website"
  - (optional) endpoints to update price / visibility / order status
  - (optional) a webhook call to us when someone pays        -> POST /webhooks/store/<id>, signed with HMAC-SHA256
Details and a copy-paste example for your site: docs/CONNECTORS.md
"""
from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx

from ..adapters.base import NormalizedItem, NormalizedOrder, ProductPayload, StoreError, country_name
from .rest_utils import dig, render

PAID = {"paid", "processing", "completed", "complete", "succeeded"}


def sign_hex(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _d(v) -> Decimal:
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return Decimal("0")


class CustomSiteStore:
    name = "custom_site"

    def __init__(self, cfg: dict, api_key: str = "", webhook_secret: str = "", client: Optional[httpx.Client] = None):
        if not cfg.get("base_url"):
            raise ValueError("base_url is required")
        self.cfg = cfg
        self.base = str(cfg["base_url"]).rstrip("/")
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0))

    def _headers(self) -> dict:
        name = (self.cfg.get("auth_header_name") or "").strip()
        h = {"Accept": "application/json"}
        if name and self.api_key:
            h[name] = str(render(self.cfg.get("auth_header_value") or "Bearer {api_key}", {"api_key": self.api_key}))
        return h

    def _call(self, method: str, path: str, **kw):
        try:
            r = self._client.request(method, self.base + path, headers=self._headers(), **kw)
        except httpx.HTTPError as e:
            raise StoreError(f"Your website is unreachable: {e}")
        if r.status_code >= 400:
            raise StoreError(f"Your website answered {method} {path} with HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            return {}

    # ---- listing ---------------------------------------------------------------------------
    def publish_product(self, p: ProductPayload) -> tuple[str, str]:
        path = self.cfg.get("publish_path") or "/api/products"
        body = {"id": p.internal_id, "title": p.title, "price": f"{p.price:.2f}", "sku": p.sku, "description": p.description,
                "image_url": p.image_url, "stock": p.stock, "category": p.category}
        data = self._call(self.cfg.get("publish_method", "POST").upper(), path, json=body)
        rid = dig(data, self.cfg.get("response_id_path") or "id")
        if rid in (None, ""):
            rid = p.internal_id  # site did not return an id: use ours, the site was given it in the body
        return str(rid), str(dig(data, self.cfg.get("response_url_path") or "url", "") or "")

    def _update(self, remote_id: str, body: dict) -> None:
        path = self.cfg.get("update_path")
        if path:
            self._call(self.cfg.get("update_method", "PATCH").upper(), str(render(path, {"remote_id": remote_id})), json=body)

    def set_product_visible(self, store_product_id: str, visible: bool) -> None:
        self._update(store_product_id, {"visible": visible})

    def set_product_price(self, store_product_id: str, price: Decimal) -> None:
        self._update(store_product_id, {"price": f"{price:.2f}"})

    # ---- orders ----------------------------------------------------------------------------
    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        if not self.webhook_secret or not signature:
            return False
        sig = signature.strip().lower().removeprefix("sha256=")
        return hmac.compare_digest(sign_hex(self.webhook_secret, raw_body), sig)

    def normalize_order(self, p: dict) -> NormalizedOrder:
        cust, ship = p.get("customer") or {}, p.get("shipping") or {}
        cc = str(ship.get("country_code") or "")
        status = str(p.get("status", "paid"))
        items = [NormalizedItem(store_product_id=str(i.get("product_id", "")), sku=str(i.get("sku") or ""), title=str(i.get("title") or ""),
                                quantity=int(i.get("quantity") or 1), unit_price=_d(i.get("unit_price")))
                 for i in p.get("items", [])]
        return NormalizedOrder(
            store_order_id=str(p["id"]), number=str(p.get("number") or p["id"]), status=status, paid=status.lower() in PAID,
            customer_name=str(cust.get("name") or ""), email=str(cust.get("email") or ""), phone=str(cust.get("phone") or ""),
            address1=str(ship.get("address1") or ""), address2=str(ship.get("address2") or ""), city=str(ship.get("city") or ""),
            state=str(ship.get("state") or ""), zip_code=str(ship.get("zip") or ""), country_code=cc,
            country=str(ship.get("country") or country_name(cc)), total=_d(p.get("total")), currency=str(p.get("currency") or ""), items=items)

    def add_order_note(self, store_order_id: str, note: str, *, customer_note: bool = False) -> None:
        path = self.cfg.get("order_update_path")
        if path:
            self._call("POST", str(render(path, {"order_id": store_order_id})), json={"note": note, "notify_customer": customer_note})

    def set_order_status(self, store_order_id: str, status: str) -> None:
        path = self.cfg.get("order_update_path")
        if path:
            self._call("PATCH", str(render(path, {"order_id": store_order_id})), json={"status": status})

    def list_recent_paid_orders(self, hours: int) -> list[dict]:
        path = self.cfg.get("orders_path")
        if not path:
            return []
        data = self._call("GET", path, params={"hours": hours})
        rows = data if isinstance(data, list) else dig(data, "orders", [])
        return rows if isinstance(rows, list) else []

    def test_connection(self) -> str:
        try:
            r = self._client.get(self.base + (self.cfg.get("health_path") or ""), headers=self._headers())
        except httpx.HTTPError as e:
            raise StoreError(f"Cannot reach {self.base}: {e}")
        if r.status_code >= 500:
            raise StoreError(f"Your website is up but returned HTTP {r.status_code}")
        if r.status_code in (401, 403):
            raise StoreError(f"Your website rejected the API key (HTTP {r.status_code})")
        return f"Your website answered (HTTP {r.status_code})."
