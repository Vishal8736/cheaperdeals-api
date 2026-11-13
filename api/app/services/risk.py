"""Order risk check. Dropshipping loses money to fake orders and chargebacks, so every paid order gets a score from 0 to 100.
A score at or above your limit sends the order to Approvals instead of to the supplier, with the reasons written out.
These are simple, explainable rules (no magic): you can see exactly why an order was held."""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Order
from . import settings_service as S

DISPOSABLE = {"mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org", "yopmail.com", "trashmail.com", "sharklasers.com",
              "getnada.com", "dispostable.com", "maildrop.cc", "throwawaymail.com", "fakeinbox.com", "mintemail.com", "mohmal.com", "emailondeck.com",
              "tempinbox.com", "mailnesia.com", "spamgourmet.com", "burnermail.io"}
FORWARDER_WORDS = ("freight forward", "forwarder", "reship", "re-ship", "mail forwarding", "package forwarding", "c/o ", "parcel hub")
DEFAULTS = {"risk_enabled": "true", "risk_hold_score": "60", "risk_blocked_countries": "", "risk_blocked_emails": ""}


def _g(db: Session, key: str) -> str:
    return S.get(db, key, DEFAULTS[key]) or DEFAULTS[key]


def settings(db: Session) -> dict:
    try:
        limit = int(_g(db, "risk_hold_score"))
    except ValueError:
        limit = 60
    return {"enabled": _g(db, "risk_enabled") == "true", "limit": limit, "countries": _g(db, "risk_blocked_countries"), "emails": _g(db, "risk_blocked_emails")}


def save_settings(db: Session, form: dict) -> list[str]:
    errs: list[str] = []
    try:
        v = int(str(form.get("risk_hold_score", "")).strip())
        if not 1 <= v <= 100:
            raise ValueError
    except ValueError:
        errs.append("Hold score must be a whole number from 1 to 100")
        v = 60
    countries = ",".join(c.strip().upper() for c in re.split(r"[,\s]+", form.get("risk_blocked_countries", "")) if c.strip())
    if any(len(c) != 2 or not c.isalpha() for c in countries.split(",") if c):
        errs.append("Blocked countries: use 2-letter codes like PK, NG")
    emails = ", ".join(e.strip().lower() for e in re.split(r"[,\n]+", form.get("risk_blocked_emails", "")) if e.strip())
    if not errs:
        S.set_value(db, "risk_enabled", "true" if form.get("risk_enabled") in ("on", "true", "1") else "false")
        S.set_value(db, "risk_hold_score", str(v))
        S.set_value(db, "risk_blocked_countries", countries)
        S.set_value(db, "risk_blocked_emails", emails)
    return errs


def assess(db: Session, order: Order) -> tuple[int, list[str]]:
    """Returns (score 0-100, reasons). Never raises: a risk-check bug must not stop a good order."""
    reasons: list[tuple[int, str]] = []
    cfg = settings(db)
    try:
        email = (order.customer_email or "").strip().lower()
        domain = email.split("@", 1)[1] if "@" in email else ""
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", email):
            reasons.append((30, "the email address is missing or invalid"))
        elif domain in DISPOSABLE:
            reasons.append((40, f"the email is a throw-away address ({domain})"))
        blocked_mail = [x.strip() for x in cfg["emails"].split(",") if x.strip()]
        if email and any(email == b or (b.startswith("@") and email.endswith(b)) or (b and "@" not in b and domain == b) for b in blocked_mail):
            reasons.append((100, "this email is on your blocked list"))
        blocked_c = [c for c in cfg["countries"].split(",") if c]
        if (order.ship_country_code or "").upper() in blocked_c:
            reasons.append((100, f"you blocked shipping to {order.ship_country_code}"))
        digits = re.sub(r"\D", "", order.customer_phone or "")
        if len(digits) < 7:
            reasons.append((15, "no usable phone number"))
        addr = f"{order.ship_address1} {order.ship_address2}".strip()
        if len(addr) < 8 or not re.search(r"\d", addr):
            reasons.append((15, "the street address looks incomplete"))
        if any(w in addr.lower() for w in FORWARDER_WORDS):
            reasons.append((25, "the address looks like a freight forwarder / re-shipper"))
        name = (order.customer_name or "").strip()
        if len(name) < 3 or re.search(r"\d", name) or re.fullmatch(r"(.)\1+", name.replace(" ", "")) or name.lower() in {"test", "asdf", "qwerty", "abc", "xyz"}:
            reasons.append((15, "the customer name looks fake"))
        since = utcnow() - timedelta(hours=24)
        recent = db.query(Order).filter(Order.id != order.id, Order.created_at >= since)
        if email and recent.filter(Order.customer_email == order.customer_email).count() >= 2:
            reasons.append((25, "3 or more orders from this email in 24 hours"))
        if addr and recent.filter(Order.ship_address1 == order.ship_address1, Order.ship_zip == order.ship_zip, Order.customer_name != order.customer_name).count() >= 1:
            reasons.append((25, "the same address was used with a different name in the last 24 hours"))
        prior = [Decimal(t) for (t,) in db.query(Order.total).filter(Order.id != order.id, Order.status != "cancelled").order_by(Order.id.desc()).limit(50)]
        if len(prior) >= 5 and Decimal(order.total) >= (sum(prior) / len(prior)) * 3:
            reasons.append((20, "the total is 3 times your usual order"))
    except Exception:  # a bug in a rule is never allowed to block orders
        return 0, []
    score = min(100, sum(p for p, _ in reasons))
    return score, [r for _, r in reasons]


def check(db: Session, order: Order) -> tuple[bool, str]:
    """Stores the score on the order. Returns (hold?, reason text)."""
    cfg = settings(db)
    if not cfg["enabled"]:
        order.risk_score, order.risk_reasons = 0, ""
        return False, ""
    score, reasons = assess(db, order)
    order.risk_score, order.risk_reasons = score, "; ".join(reasons)
    if reasons and score >= cfg["limit"]:
        return True, f"Risk score {score}/100 (limit {cfg['limit']}): " + "; ".join(reasons)
    return False, ""
