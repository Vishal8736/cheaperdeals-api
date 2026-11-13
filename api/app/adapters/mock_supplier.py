"""In-memory supplier used for local demo, DRY-RUN rehearsals and tests. Supports failure injection."""
from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Optional

from .base import (
    CatalogItem, SupplierError, SupplierOrderInfo, SupplierOrderRequest, SupplierOrderResult, SupplierProductInfo,
)


DEMO_CATALOG = [
    # (title, cost, stock, category) - image is a generated SVG so it needs no internet
    ("Magnetic Phone Car Mount", "3.20", 240, "Car"), ("LED Strip Lights 5m RGB", "4.10", 180, "Home"),
    ("Wireless Earbuds Pro", "9.80", 75, "Audio"), ("Portable Blender Bottle", "7.40", 60, "Kitchen"),
    ("Silicone Kitchen Utensil Set", "5.90", 120, "Kitchen"), ("Smart Fitness Band", "11.30", 45, "Wearables"),
    ("Mini Desk Humidifier", "6.20", 90, "Home"), ("Foldable Laptop Stand", "8.70", 130, "Office"),
    ("Pet Grooming Glove", "2.60", 300, "Pets"), ("USB-C Fast Charger 65W", "10.50", 85, "Electronics"),
    ("Yoga Mat Anti-Slip", "9.10", 70, "Fitness"), ("Waterproof Phone Pouch", "1.90", 400, "Outdoor"),
]


def demo_image(title: str, n: int) -> str:
    from urllib.parse import quote
    hues = [265, 190, 330, 150, 25, 215]
    h = hues[n % len(hues)]
    initials = "".join(w[0] for w in title.split()[:2]).upper()
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'><rect width='200' height='200' fill='hsl({h},70%,92%)'/>"
           f"<circle cx='100' cy='88' r='46' fill='hsl({h},65%,58%)'/><text x='100' y='104' font-family='sans-serif' font-size='42' "
           f"font-weight='700' text-anchor='middle' fill='white'>{initials}</text></svg>")
    return "data:image/svg+xml;utf8," + quote(svg)


class MockSupplier:
    name = "mock"

    def __init__(self, supports_lookup: bool = True):
        self.supports_lookup = supports_lookup
        self.orders: dict[str, dict] = {}  # supplier_order_id -> {order_number, status, tracking}
        self.by_number: dict[str, str] = {}
        self.products: dict[str, dict] = {}  # vid -> {stock, price}
        self.calls: list[str] = []
        self.fail_next: list[SupplierError] = []  # consumed one per create_order call
        self.lose_response_next = 0  # order IS created but we raise a timeout (the nasty case)
        self._ids = itertools.count(1000)

    # --- test / demo helpers
    def set_product(self, vid: str, stock: int, price: Decimal | float | str) -> None:
        self.products[vid] = {"stock": int(stock), "price": Decimal(str(price))}

    def advance(self, supplier_order_id: str, status: str, tracking: str = "", carrier: str = "") -> None:
        o = self.orders[supplier_order_id]
        o["status"] = status
        if tracking:
            o["tracking"] = tracking
            o["carrier"] = carrier or "MockExpress"

    def list_catalog(self, query: str = "", page: int = 1, page_size: int = 20) -> list[CatalogItem]:
        rows = [(i, r) for i, r in enumerate(DEMO_CATALOG) if query.lower() in r[0].lower() or query.lower() in r[3].lower()]
        rows = rows[(page - 1) * page_size: page * page_size]
        out = []
        for i, (title, cost, stock, cat) in rows:
            vid = f"DEMO-{i + 1:03d}"
            self.set_product(vid, stock, cost)
            out.append(CatalogItem(supplier_product_id=f"P-{i + 1:03d}", vid=vid, title=title, cost=Decimal(cost), stock=stock,
                                   sku=f"SKU-{i + 1:03d}", image_url=demo_image(title, i), category=cat,
                                   description=f"{title}. Demo product from the built-in mock supplier."))
        return out

    def test_connection(self) -> str:
        return "Mock supplier is always available (demo catalog of 12 products)."

    # --- contract
    def create_order(self, req: SupplierOrderRequest) -> SupplierOrderResult:
        self.calls.append("create_order")
        if self.fail_next:
            raise self.fail_next.pop(0)
        if req.order_number in self.by_number:
            raise SupplierError("duplicate orderNumber")
        oid = f"MOCK-{next(self._ids)}"
        self.orders[oid] = {"order_number": req.order_number, "status": "confirmed", "tracking": "", "carrier": ""}
        self.by_number[req.order_number] = oid
        if self.lose_response_next > 0:
            self.lose_response_next -= 1
            raise SupplierError("mock timeout after send", ambiguous=True)
        return SupplierOrderResult(supplier_order_id=oid, status="confirmed", raw={"mock": True})

    def find_order(self, order_number: str) -> Optional[SupplierOrderResult]:
        self.calls.append("find_order")
        oid = self.by_number.get(order_number)
        return SupplierOrderResult(supplier_order_id=oid, status=self.orders[oid]["status"]) if oid else None

    def get_order_info(self, supplier_order_id: str) -> SupplierOrderInfo:
        self.calls.append("get_order_info")
        o = self.orders.get(supplier_order_id)
        if not o:
            raise SupplierError(f"unknown supplier order {supplier_order_id}")
        return SupplierOrderInfo(supplier_order_id=supplier_order_id, status=o["status"], raw_status=o["status"],
                                 tracking_number=o["tracking"], carrier=o["carrier"])

    def get_product_info(self, vid: str) -> SupplierProductInfo:
        self.calls.append("get_product_info")
        p = self.products.get(vid)
        if not p:
            raise SupplierError(f"unknown vid {vid}")
        return SupplierProductInfo(vid=vid, stock=p["stock"], price=p["price"])
