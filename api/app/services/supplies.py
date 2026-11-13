"""Supplier wallet watch: CJ pays orders from its own balance, and an empty balance silently stops every order."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from ..db import utcnow
from . import settings_service as S


def cj_balance_check(db, rt, *, force: bool = False) -> str:
    """At most every 6 hours. Returns a short note for the job log ('' when nothing was checked)."""
    last = S.get(db, "cj_balance_checked", "")
    if not force and last:
        try:
            from datetime import datetime
            if utcnow() - datetime.fromisoformat(last) < timedelta(hours=6):
                return ""
        except ValueError:
            pass
    try:
        sup = rt.supplier_for(None)
    except Exception:
        return ""
    fn = getattr(sup, "get_balance", None)
    if fn is None:
        return ""
    S.set_value(db, "cj_balance_checked", utcnow().isoformat(timespec="seconds"))
    try:
        bal = Decimal(fn())
    except Exception as e:
        S.set_value(db, "cj_balance_last", f"error: {str(e)[:120]}")
        return "CJ balance padh nahi paya"
    S.set_value(db, "cj_balance_last", str(bal))
    try:
        floor = Decimal(S.get(db, "cj_min_balance", "20") or "20")
    except Exception:
        floor = Decimal("20")
    if bal < floor:
        rt.alert(db, level="warning", source="supplier", message=f"CJ wallet mein sirf ${bal} bacha hai (limit ${floor}). Paisa add karo, warna naye orders CJ par fail honge.",
                 dedupe_key="cj-low-balance", dedupe_hours=12)
    return f"CJ balance ${bal}"
