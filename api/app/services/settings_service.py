from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from ..config import Config
from ..db import utcnow
from ..models import Setting

# key -> (default, human label, help)
DEFINITIONS: dict[str, tuple[str, str, str]] = {
    "min_margin_pct": ("20", "Minimum margin (%)", "Orders below this margin are blocked. Products can override it."),
    "markup_pct": ("60", "Auto-pricing markup (%)", "When the supplier price changes, selling price = supplier price + this markup."),
    "approval_threshold": ("100", "Approval threshold", "Orders with a total at or above this wait for your approval. 0 turns it off."),
    "daily_order_cap": ("20", "Daily order cap", "Max orders sent to the supplier per day. 0 means no cap."),
    "max_retries": ("5", "Max retries", "Supplier retries (with growing delay) before the order needs you."),
    "retry_base_minutes": ("2", "Retry base delay (minutes)", "Delay doubles each retry: 2, 4, 8, 16, 32 minutes."),
    "default_logistic_name": ("", "Shipping method name", "CJ shipping method name sent with each order, e.g. CJPacket Ordinary. Write AUTO to let the app pick the cheapest method CJ offers for each order. Required before you go live."),
    "price_ending": ("", "Price ending", "Round selling prices up to end in this, e.g. 99 turns 14.56 into 14.99. Empty = off."),
    "outgoing_webhook_url": ("", "Outgoing webhook address", "We POST events (order placed/shipped, product listed, failover) here as JSON, signed. Empty = off."),
    "outgoing_webhook_secret": ("", "Outgoing webhook secret", "Signs each call: header X-CheapeDeal-Signature = HMAC-SHA256 hex of the body."),
    "from_country_code": ("CN", "Ship-from country code", "Country the goods ship from (2 letters). CJ: CN (China warehouse) is the usual."),
}
FLAGS = {"paused": "false"}  # dry_run default comes from env on first boot


def ensure_defaults(db: Session, cfg: Config) -> None:
    existing = {s.key for s in db.query(Setting).all()}
    for k, (v, _, _) in DEFINITIONS.items():
        if k not in existing:
            db.add(Setting(key=k, value=v))
    if "paused" not in existing:
        db.add(Setting(key="paused", value="false" if cfg.app_env == "test" else "true"))  # a real install waits for you to press Start
    if "feed_token" not in existing:
        import secrets as _sec
        db.add(Setting(key="feed_token", value=_sec.token_urlsafe(24)))
    if "business_mode" not in existing:
        db.add(Setting(key="business_mode", value=getattr(cfg, "business_mode", "affiliate")))
    on = {k.strip() for k in (getattr(cfg, "biz_on", "") or "").split(",") if k.strip()}  # BIZ_ON env: which businesses start switched on
    if not on:
        on = {"affiliate"} | ({"dropship", "pod"} if getattr(cfg, "business_mode", "affiliate") == "dropship" else set())
    for k in ("affiliate", "pod", "dropship"):
        if f"biz_{k}_on" not in existing:
            db.add(Setting(key=f"biz_{k}_on", value="true" if k in on else "false"))
    if "dry_run" not in existing:
        db.add(Setting(key="dry_run", value="true" if cfg.dry_run_default else "false"))
    db.commit()


def get(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    if row is not None:
        return row.value
    if key in DEFINITIONS:
        return DEFINITIONS[key][0]
    return default


def set_value(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.flush()


def get_bool(db: Session, key: str) -> bool:
    return get(db, key, "false").strip().lower() in {"1", "true", "yes", "on"}


def get_int(db: Session, key: str) -> int:
    try:
        return int(Decimal(get(db, key, "0") or "0"))
    except Exception:
        return int(DEFINITIONS.get(key, ("0",))[0] or 0)


def get_decimal(db: Session, key: str) -> Decimal:
    try:
        return Decimal(get(db, key, "0") or "0")
    except Exception:
        return Decimal(DEFINITIONS.get(key, ("0",))[0] or "0")


def mark_run(db: Session, job: str, summary: str = "") -> None:
    set_value(db, f"last_run:{job}", f"{utcnow().isoformat()}|{summary}")


def last_run(db: Session, job: str) -> tuple[datetime | None, str]:
    raw = get(db, f"last_run:{job}", "")
    if not raw:
        return None, ""
    ts, _, summary = raw.partition("|")
    try:
        return datetime.fromisoformat(ts), summary
    except ValueError:
        return None, summary


def validate_and_save(db: Session, form: dict[str, str]) -> list[str]:
    """Validate numeric settings from the Settings page; returns a list of error strings (nothing saved on error)."""
    errors: list[str] = []
    clean: dict[str, str] = {}
    for key in ("min_margin_pct", "markup_pct", "approval_threshold"):
        try:
            v = Decimal(form.get(key, "").strip())
            if v < 0 or (key == "min_margin_pct" and v >= 100):
                raise ValueError
            clean[key] = str(v)
        except Exception:
            errors.append(f"{DEFINITIONS[key][1]} must be a number (margin below 100, others 0 or more)")
    for key in ("daily_order_cap", "max_retries", "retry_base_minutes"):
        try:
            v = int(form.get(key, "").strip())
            if v < 0 or (key != "daily_order_cap" and v < 1):
                raise ValueError
            clean[key] = str(v)
        except Exception:
            errors.append(f"{DEFINITIONS[key][1]} must be a whole number")
    pe = form.get("price_ending", "").strip()
    if pe and not (pe.isdigit() and 0 <= int(pe) <= 99):
        errors.append("Price ending must be a number from 0 to 99, or empty")
    else:
        clean["price_ending"] = pe
    wu = form.get("outgoing_webhook_url", "").strip()
    if wu and not wu.lower().startswith(("http://", "https://")):
        errors.append("Outgoing webhook address must start with http:// or https://")
    else:
        clean["outgoing_webhook_url"] = wu
    clean["outgoing_webhook_secret"] = form.get("outgoing_webhook_secret", "").strip()
    clean["default_logistic_name"] = form.get("default_logistic_name", "").strip()
    cc = form.get("from_country_code", "").strip().upper()
    if len(cc) != 2 or not cc.isalpha():
        errors.append("Ship-from country code must be 2 letters, like CN")
    else:
        clean["from_country_code"] = cc
    if not errors:
        for k, v in clean.items():
            set_value(db, k, v)
    return errors
