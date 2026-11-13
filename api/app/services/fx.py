"""Exchange rates. Everything inside Cheape Deal (supplier cost, selling price, profit) is in USD.
A rate says how many units of a currency one US dollar buys. Rates start as rough SEED values: they work for trying things out, but the
Strategy agent will not change a live price from a seed rate until you confirm it (save the rate yourself, or press Refresh)."""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import httpx
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import FxRate

log = logging.getLogger("cheapedeal.fx")
FETCHER = None  # tests replace this: FETCHER() -> {"INR": 83.1, ...}
RATES_URL = "https://open.er-api.com/v6/latest/USD"  # free, no key. If it ever changes, rates simply stay as they are.

SEED = {"USD": "1", "INR": "84", "EUR": "0.92", "GBP": "0.78", "AED": "3.6725", "AUD": "1.52", "CAD": "1.37", "SAR": "3.75",
        "CNY": "7.2", "SGD": "1.34", "JPY": "150", "TRY": "34", "BRL": "5.6", "MXN": "18.5", "ZAR": "18", "PLN": "4", "SEK": "10.5"}


class FxError(ValueError):
    pass


def ensure_seed(db: Session) -> None:
    have = {r.code for r in db.query(FxRate.code).all()}
    for code, v in SEED.items():
        if code not in have:
            db.add(FxRate(code=code, per_usd=Decimal(v), source="manual" if code == "USD" else "seed"))
    db.flush()


def norm(code: str) -> str:
    c = (code or "").strip().upper()
    return c or "USD"


def get(db: Session, code: str) -> FxRate | None:
    return db.get(FxRate, norm(code))


def rate(db: Session, code: str) -> Decimal:
    r = get(db, code)
    if r is None or Decimal(r.per_usd) <= 0:
        raise FxError(f"No exchange rate for {norm(code)}. Add it on the Strategy page.")
    return Decimal(r.per_usd)


def confirmed(db: Session, code: str) -> bool:
    r = get(db, code)
    return norm(code) == "USD" or (r is not None and r.source != "seed")


def to_usd(db: Session, amount, code: str) -> Decimal:
    return (Decimal(amount) / rate(db, code)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def from_usd(db: Session, amount, code: str) -> Decimal:
    return (Decimal(amount) * rate(db, code)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def set_rate(db: Session, code: str, per_usd: str, source: str = "manual") -> None:
    code = norm(code)
    if not (len(code) == 3 and code.isalpha()):
        raise FxError("A currency code has 3 letters, like INR")
    try:
        v = Decimal(str(per_usd).strip())
    except Exception:
        raise FxError(f"'{per_usd}' is not a number")
    if v <= 0 or v > Decimal("1000000"):
        raise FxError("The rate must be above 0")
    if code == "USD" and v != 1:
        raise FxError("USD is the base currency; its rate is always 1")
    r = get(db, code)
    if r is None:
        db.add(FxRate(code=code, per_usd=v, source=source, updated_at=utcnow()))
    else:
        r.per_usd, r.source, r.updated_at = v, source, utcnow()
    db.flush()


def _fetch() -> dict:
    if FETCHER is not None:
        return FETCHER()
    resp = httpx.get(RATES_URL, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    return data.get("rates") or {}


def refresh(db: Session) -> dict:
    """Update every known currency from the free rates feed. Hand-typed (manual) rates are never overwritten.
    Never raises: on any failure the old rates stay and the result says why."""
    try:
        feed = _fetch()
    except Exception as e:
        log.warning("rate refresh failed: %s", e)
        return {"ok": False, "updated": 0, "error": f"Could not reach the rates feed ({type(e).__name__})"}
    n = 0
    for r in db.query(FxRate).all():
        if r.code == "USD" or r.source == "manual" or r.code not in feed:
            continue
        try:
            v = Decimal(str(feed[r.code]))
        except Exception:
            continue
        if v > 0:
            r.per_usd, r.source, r.updated_at = v, "auto", utcnow()
            n += 1
    db.flush()
    return {"ok": True, "updated": n, "error": ""}


def refresh_if_due(db: Session, max_age_hours: int = 24) -> dict | None:
    newest = max((r.updated_at for r in db.query(FxRate).filter(FxRate.source == "auto")), default=None)
    if newest is not None and utcnow() - newest < timedelta(hours=max_age_hours):
        return None
    return refresh(db)
