"""Website settings shared by shop.py and payments.py (kept apart so they can import each other's helpers without loops)."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import httpx
from sqlalchemy.orm import Session

from . import settings_service as S

SECRET_KEY = ""  # set by main.create_app
HTTP = None  # tests replace this with an httpx.Client on a MockTransport; real runs make a fresh client per call

DEFAULTS = {
    "shop_name": "Cheaperdeal",
    "shop_tagline": "Smart deals. Real savings.",
    "shop_announce": "",
    "shop_currency": "USD",  # the currency the website opens in
    "shop_currencies": "USD,INR,EUR,GBP,AED,AUD,CAD",  # currencies a customer may pick (each needs an exchange rate: Strategy page)
    "shop_ship_flat": "4.99",  # USD
    "shop_free_over": "35",  # USD
    "shop_methods": "manual",  # comma list: manual, upi, razorpay
    "shop_pay_note": "Order place karne ke baad hum aapko payment ka tarika email/WhatsApp par bhejenge. Payment aate hi order aage badhta hai.",
    "shop_countries": "US,IN,GB,CA,AU,AE",
    "shop_support_email": "",
    "shop_delivery_days": "10-25",
    "shop_address": "",
    "shop_site_url": "",  # the website address (https://yourshop.vercel.app): used in links inside emails
    "shop_on": "true",
    "razorpay_key_id": "",
    "upi_vpa": "", "upi_name": "",
    "cj_min_balance": "20",
}
SYMBOLS = {"USD": "$", "INR": "₹", "GBP": "£", "EUR": "€", "AED": "AED ", "AUD": "A$", "CAD": "C$", "JPY": "¥", "CNY": "¥", "SGD": "S$"}


def configure(secret_key: str) -> None:
    global SECRET_KEY
    SECRET_KEY = secret_key


def get(db: Session, key: str) -> str:
    return S.get(db, key, DEFAULTS.get(key, ""))


def money(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def symbol(code: str) -> str:
    return SYMBOLS.get(code, code + " ")


def save_secret(db: Session, key: str, value: str) -> None:
    from ..secretbox import seal
    S.set_value(db, key, seal(SECRET_KEY, {"s": value}) if value else "")


def read_secret(db: Session, key: str) -> str:
    enc = S.get(db, key, "")
    if not enc:
        return ""
    try:
        from ..secretbox import unseal
        return unseal(SECRET_KEY, enc).get("s", "")
    except Exception:
        return ""


def client() -> httpx.Client:
    return HTTP or httpx.Client(timeout=15.0, follow_redirects=True)
