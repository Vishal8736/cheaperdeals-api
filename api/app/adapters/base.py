"""Adapter contracts. Business logic only talks to these, never to CJ / WooCommerce directly."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Protocol


COUNTRY_NAMES = {
    "IN": "India", "US": "United States", "GB": "United Kingdom", "CA": "Canada", "AU": "Australia",
    "AE": "United Arab Emirates", "DE": "Germany", "FR": "France", "IT": "Italy", "ES": "Spain",
    "NL": "Netherlands", "SG": "Singapore", "SA": "Saudi Arabia", "NZ": "New Zealand", "PK": "Pakistan",
    "BD": "Bangladesh", "LK": "Sri Lanka", "NP": "Nepal", "ZA": "South Africa", "BR": "Brazil",
    "MX": "Mexico", "JP": "Japan", "MY": "Malaysia", "ID": "Indonesia", "PH": "Philippines",
}


def country_name(code: str) -> str:
    return COUNTRY_NAMES.get((code or "").upper(), code or "")


class SupplierError(Exception):
    """Raised by supplier adapters.

    retryable  - safe to try again later (429, 5xx before the order was accepted).
    ambiguous  - we cannot know whether the supplier created the order (timeout / connection lost
                 after the request was sent). Must NOT be blindly retried.
    """

    def __init__(self, message: str, *, retryable: bool = False, ambiguous: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.status_code = status_code


class StoreError(Exception):
    pass


@dataclass
class SupplierLine:
    vid: str
    quantity: int
    sku: str = ""


@dataclass
class SupplierOrderRequest:
    order_number: str
    customer_name: str
    email: str
    phone: str
    address1: str
    address2: str
    city: str
    province: str
    zip_code: str
    country_code: str
    country: str
    logistic_name: str
    from_country_code: str
    lines: list[SupplierLine]
    remark: str = ""


@dataclass
class SupplierOrderResult:
    supplier_order_id: str
    status: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class SupplierOrderInfo:
    """Normalised view of the supplier's order state. status in: pending | confirmed | shipped | delivered | cancelled"""
    supplier_order_id: str
    status: str
    raw_status: str = ""
    tracking_number: str = ""
    carrier: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class SupplierProductInfo:
    vid: str
    stock: int
    price: Decimal


@dataclass
class CatalogItem:
    """One sellable product as a supplier shows it. Every supplier connector converts its own data into this."""
    supplier_product_id: str
    vid: str  # the variant id used when ordering
    title: str
    cost: Decimal
    stock: int = 0
    sku: str = ""
    image_url: str = ""
    description: str = ""
    category: str = ""


@dataclass
class ProductPayload:
    """What a store connector needs to publish a product on a website."""
    title: str
    price: Decimal
    sku: str = ""
    description: str = ""
    image_url: str = ""
    stock: int = 0
    category: str = ""
    internal_id: str = ""  # Cheape Deal's own product id, so the website can map it back


class SupplierAdapter(Protocol):
    name: str
    supports_lookup: bool  # can we ask "does an order with this orderNumber exist?"

    def create_order(self, req: SupplierOrderRequest) -> SupplierOrderResult: ...
    def find_order(self, order_number: str) -> Optional[SupplierOrderResult]: ...
    def get_order_info(self, supplier_order_id: str) -> SupplierOrderInfo: ...
    def get_product_info(self, vid: str) -> SupplierProductInfo: ...
    # optional (needed only to import products): def list_catalog(self, query: str, page: int, page_size: int) -> list[CatalogItem]
    # optional: def test_connection(self) -> str


@dataclass
class NormalizedItem:
    store_product_id: str
    sku: str
    title: str
    quantity: int
    unit_price: Decimal


@dataclass
class NormalizedOrder:
    """Store-agnostic order. Every StoreAdapter converts its own payload into this."""
    store_order_id: str
    number: str
    status: str  # store-side status, e.g. woo: processing
    paid: bool
    customer_name: str
    email: str
    phone: str
    address1: str
    address2: str
    city: str
    state: str
    zip_code: str
    country_code: str
    country: str
    total: Decimal
    currency: str
    items: list[NormalizedItem]


class StoreAdapter(Protocol):
    name: str

    def verify_webhook(self, raw_body: bytes, signature: str) -> bool: ...
    def normalize_order(self, payload: dict) -> NormalizedOrder: ...
    def add_order_note(self, store_order_id: str, note: str, *, customer_note: bool = False) -> None: ...
    def set_order_status(self, store_order_id: str, status: str) -> None: ...
    def set_product_visible(self, store_product_id: str, visible: bool) -> None: ...
    def set_product_price(self, store_product_id: str, price: Decimal) -> None: ...
    def list_recent_paid_orders(self, hours: int) -> list[dict]: ...
    # optional (needed only to list products on the site): def publish_product(self, p: ProductPayload) -> tuple[str, str]  # (remote_id, url)
    # optional: def test_connection(self) -> str


class Notifier(Protocol):
    def send(self, text: str) -> bool: ...
