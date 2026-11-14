"""Stand-in used when NO real supplier is connected. It never pretends: every call fails with a clear message, so an order is held (not 'ordered' with an
invented supplier id) and the Catalog agent never lists demo products. The old built-in fallback was a simulated supplier."""
from __future__ import annotations

from .base import SupplierError

MESSAGE = "Supplier connect nahi hai: API Keys page par CJ API key daalo (ya Connect page par supplier jodo). Tab tak koi order/product nakli tareeke se nahi chalega."


class NoSupplier:
    name = "No supplier connected"
    supports_lookup = False

    def _no(self, *a, **k):
        raise SupplierError(MESSAGE, retryable=False)

    create_order = find_order = get_order_info = get_product_info = list_catalog = shipping_options = variants_of = _no

    def test_connection(self) -> str:
        raise SupplierError(MESSAGE, retryable=False)
