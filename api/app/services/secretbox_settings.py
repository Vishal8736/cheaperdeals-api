"""The admin's 2FA secret lives sealed (encrypted with SECRET_KEY) in the settings table."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import secretbox
from . import settings_service as S


def totp_secret(db: Session, cfg) -> str:
    """The active secret, or "" when 2FA is off."""
    if not S.get_bool(db, "totp_enabled"):
        return ""
    return _read(db, cfg, "totp_secret_enc")


def _read(db: Session, cfg, key: str) -> str:
    enc = S.get(db, key, "")
    if not enc:
        return ""
    try:
        return secretbox.unseal(cfg.secret_key, enc).get("s", "")
    except Exception:
        return ""


def pending_secret(db: Session, cfg) -> str:
    return _read(db, cfg, "totp_pending_enc")


def set_pending(db: Session, cfg, secret: str) -> None:
    S.set_value(db, "totp_pending_enc", secretbox.seal(cfg.secret_key, {"s": secret}))


def activate(db: Session, cfg, secret: str) -> None:
    S.set_value(db, "totp_secret_enc", secretbox.seal(cfg.secret_key, {"s": secret}))
    S.set_value(db, "totp_enabled", "true")
    S.set_value(db, "totp_pending_enc", "")


def disable(db: Session) -> None:
    S.set_value(db, "totp_enabled", "false")
    S.set_value(db, "totp_secret_enc", "")
    S.set_value(db, "totp_pending_enc", "")
