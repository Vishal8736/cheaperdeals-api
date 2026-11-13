"""In-memory store for local demo and tests."""
from __future__ import annotations

from decimal import Decimal

from .base import NormalizedItem, NormalizedOrder, ProductPayload


class MockStore:
    name = "mock"

    def __init__(self, webhook_secret: str = "mock-secret"):
        self.webhook_secret = webhook_secret
        self.notes: list[tuple[str, str, bool]] = []
        self.statuses: dict[str, str] = {}
        self.visible: dict[str, bool] = {}
        self.prices: dict[str, Decimal] = {}
        self.recent_orders: list[dict] = []
        self.published: dict[str, ProductPayload] = {}

    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        from .woocommerce import sign
        return signature == sign(self.webhook_secret, raw_body)

    def normalize_order(self, p: dict) -> NormalizedOrder:
        return NormalizedOrder(
            store_order_id=str(p["id"]), number=str(p.get("number", p["id"])), status=p.get("status", "processing"),
            paid=p.get("status", "processing") in {"processing", "completed"},
            customer_name=p.get("name", ""), email=p.get("email", ""), phone=p.get("phone", ""),
            address1=p.get("address1", ""), address2=p.get("address2", ""), city=p.get("city", ""),
            state=p.get("state", ""), zip_code=p.get("zip", ""), country_code=p.get("country_code", ""),
            country=p.get("country", ""), total=Decimal(str(p.get("total", "0"))), currency=p.get("currency", ""),
            items=[NormalizedItem(str(i["store_product_id"]), i.get("sku", ""), i.get("title", ""),
                                  int(i.get("quantity", 1)), Decimal(str(i["unit_price"]))) for i in p.get("items", [])],
        )

    def add_order_note(self, store_order_id, note, *, customer_note=False):
        self.notes.append((store_order_id, note, customer_note))

    def set_order_status(self, store_order_id, status):
        self.statuses[store_order_id] = status

    def set_product_visible(self, store_product_id, visible):
        self.visible[store_product_id] = visible

    def set_product_price(self, store_product_id, price):
        self.prices[store_product_id] = price

    def list_recent_paid_orders(self, hours):
        return list(self.recent_orders)

    def publish_product(self, p: ProductPayload) -> tuple[str, str]:
        rid = f"mock-{len(self.published) + 1}"
        self.published[rid] = p
        return rid, ""

    def test_connection(self) -> str:
        return "Mock store is always available."
