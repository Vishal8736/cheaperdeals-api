"""Base classes for connector FILES. Copy connectors/examples/*.py, rename, fill in the methods.

A connector file defines one class that extends SupplierConnector (a company you buy from) or StoreConnector
(a website you sell on). Cheape Deal finds it automatically, shows its settings form in Connect, and stores API keys
encrypted. You only write the calls to the company's SDK or API.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import httpx

from ..adapters.base import (  # noqa: F401  (re-exported for connector files)
    CatalogItem, NormalizedItem, NormalizedOrder, ProductPayload, StoreError, SupplierError, SupplierLine,
    SupplierOrderInfo, SupplierOrderRequest, SupplierOrderResult, SupplierProductInfo,
)


@dataclass
class Field:
    """One input in the connection form."""
    key: str
    label: str
    secret: bool = False  # masked in the UI and encrypted at rest
    required: bool = True
    help: str = ""
    placeholder: str = ""
    default: str = ""
    textarea: bool = False


class _Base:
    key: str = ""  # unique short id, letters/digits/underscore, e.g. "acme"
    label: str = ""  # shown in the UI
    description: str = ""
    fields: list[Field] = []
    name: str = "custom"

    def __init__(self, config: dict, secrets: dict):
        self.config = config
        self.secrets = secrets

    # tiny HTTP helper so connector files stay short
    def http(self, method: str, url: str, *, headers: Optional[dict] = None, timeout: float = 20.0, error=SupplierError, **kw):
        try:
            r = httpx.request(method, url, headers=headers, timeout=timeout, **kw)
        except httpx.TimeoutException as e:
            raise error(f"{self.label or self.key} timed out: {e}")
        except httpx.HTTPError as e:
            raise error(f"{self.label or self.key} unreachable: {e}")
        if r.status_code >= 400:
            raise error(f"{self.label or self.key} {method} -> HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            return {"_text": r.text}

    def test_connection(self) -> str:
        """Return a short success message, or raise an exception with a clear reason."""
        return "No test implemented for this connector."


class SupplierConnector(_Base):
    """A company you buy products from. Implement what you need; catalog import needs list_catalog,
    automatic orders need create_order + get_order_info, stock/price sync needs get_product_info."""
    supports_lookup = False  # True only if find_order() can check whether an order number already exists

    def list_catalog(self, query: str = "", page: int = 1, page_size: int = 20) -> list[CatalogItem]:
        raise NotImplementedError("This connector cannot list products (list_catalog is not implemented).")

    def get_product_info(self, vid: str) -> SupplierProductInfo:
        raise SupplierError("This connector has no stock/price lookup (get_product_info is not implemented).")

    def create_order(self, req: SupplierOrderRequest) -> SupplierOrderResult:
        raise SupplierError("This connector cannot place orders (create_order is not implemented).")

    def find_order(self, order_number: str) -> Optional[SupplierOrderResult]:
        return None

    def get_order_info(self, supplier_order_id: str) -> SupplierOrderInfo:
        raise SupplierError("This connector cannot read order status (get_order_info is not implemented).")


class StoreConnector(_Base):
    """A website you sell on. publish_product puts a product on it; the rest are optional."""

    def publish_product(self, p: ProductPayload) -> tuple[str, str]:
        raise StoreError("This connector cannot publish products (publish_product is not implemented).")

    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        return False

    def normalize_order(self, payload: dict) -> NormalizedOrder:
        raise StoreError("This connector does not receive orders (normalize_order is not implemented).")

    def add_order_note(self, store_order_id: str, note: str, *, customer_note: bool = False) -> None:
        return None

    def set_order_status(self, store_order_id: str, status: str) -> None:
        return None

    def set_product_visible(self, store_product_id: str, visible: bool) -> None:
        return None

    def set_product_price(self, store_product_id: str, price: Decimal) -> None:
        return None

    def list_recent_paid_orders(self, hours: int) -> list[dict]:
        return []
