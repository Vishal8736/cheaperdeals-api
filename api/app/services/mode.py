"""Which businesses are switched on (v19/v20). The project is a set of separate businesses, each with its own ON/OFF:
affiliate (partner links + network APIs), pod (print-on-demand: designs made here, printed and shipped by Printful/Printify), dropship (a supplier
ships), ebook (later). `sells` means a business that takes orders on our own checkout is on (pod or dropship); with neither on, the website has no
cart and every product is a "Buy at <merchant>" link. Older code asks `is_affiliate()` / `current()`: kept, defined from the flags."""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import settings_service as S

DEFAULT = "affiliate"
KEYS = ("affiliate", "pod", "dropship", "ebook")
# jobs that only make sense when we take and fulfil orders ourselves
DROPSHIP_JOBS = {"process_pending", "intake_sweep", "tracking_sync", "stock_price_sync", "reconcile", "strategy_run", "price_watch",
                 "playbook_run", "catalog_run", "shop_watch"}
# the one business a job belongs to (jobs not listed here follow `sells`)
JOB_OWNER = {"affiliate_run": "affiliate", "earnings_run": "affiliate", "pod_run": "pod", "catalog_run": "dropship"}
# nav keys hidden when no selling business is on
DROPSHIP_NAV = {"orders", "approvals", "strategy", "finance", "returns", "risk", "playbook", "catalog", "support"}


def is_on(db: Session, key: str) -> bool:
    if key not in KEYS or key == "ebook":  # ebook is not built yet: it can never be ON
        return False
    return S.get(db, f"biz_{key}_on", "true" if key == "affiliate" else "false") == "true"


def set_on(db: Session, key: str, on: bool) -> bool:
    if key not in KEYS or key == "ebook":
        return False
    S.set_value(db, f"biz_{key}_on", "true" if on else "false")
    return True


def sells(db: Session) -> bool:
    return is_on(db, "dropship") or is_on(db, "pod")


def current(db: Session) -> str:
    return "dropship" if sells(db) else "affiliate"


def is_affiliate(db: Session) -> bool:
    return not sells(db)


def set_mode(db: Session, mode: str) -> str:
    """Legacy switch (Settings): dropship = every selling business on, affiliate = selling businesses off."""
    on = mode == "dropship"
    set_on(db, "dropship", on)
    set_on(db, "pod", on)
    S.set_value(db, "business_mode", "dropship" if on else "affiliate")
    return "dropship" if on else "affiliate"


def job_allowed(db: Session, job: str) -> bool:
    owner = JOB_OWNER.get(job)
    if owner:
        return is_on(db, owner)
    if job in DROPSHIP_JOBS:
        return sells(db)
    return True


def nav_hide(db: Session) -> list[str]:
    hide = set() if sells(db) else set(DROPSHIP_NAV)
    if not is_on(db, "pod"):
        hide.add("pod")
    if not is_on(db, "affiliate"):
        hide |= {"earnings"}
    return sorted(hide)
