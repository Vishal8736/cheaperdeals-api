"""Selling-price rule: cost + markup, optionally rounded UP to a price ending such as .99."""
from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal


def sell_price(cost: Decimal, markup_pct: Decimal, ending: str = "") -> Decimal:
    price = (Decimal(cost) * (1 + Decimal(markup_pct) / 100)).quantize(Decimal("0.01"))
    ending = (ending or "").strip()
    if not ending.isdigit() or price <= 0:
        return price
    e = Decimal(int(ending)) / 100
    out = price.to_integral_value(rounding=ROUND_FLOOR) + e
    if out < price:  # never round DOWN: that would eat the margin
        out += 1
    return out.quantize(Decimal("0.01"))
