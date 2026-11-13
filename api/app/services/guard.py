"""Compliance guard: never list or price products whose name or category contains a word you have banned
(brands you may not resell, weapons, medicines, anything your country, your store or your payment provider forbids)."""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from . import settings_service as S

DEFAULT_NOTE = "Comma-separated words. A product whose title or category contains one is never priced or listed automatically."


def words(db: Session) -> list[str]:
    return [w.strip().lower() for w in re.split(r"[,\n]", S.get(db, "blocked_keywords", "") or "") if w.strip()]


def blocked_reason(db: Session, *texts: str) -> str:
    hay = " ".join(t or "" for t in texts).lower()
    for w in words(db):
        if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", hay):
            return f"Blocked by your compliance list: the word '{w}' is not allowed (Settings > Rules)"
    return ""
