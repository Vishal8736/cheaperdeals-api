from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..adapters.base import Notifier
from ..db import utcnow
from ..models import Alert, AuditLog, Order


def audit(db: Session, entity: str, entity_id: int, action: str, *, from_state: str = "", to_state: str = "",
          detail: str = "", actor: str = "system") -> None:
    db.add(AuditLog(entity=entity, entity_id=entity_id, action=action, from_state=from_state,
                    to_state=to_state, detail=detail[:2000], actor=actor))


def raise_alert(db: Session, notifier: Notifier, *, level: str, source: str, message: str,
                order_id: Optional[int] = None, dedupe_key: str = "", dedupe_hours: int = 6,
                public_url: str = "") -> Optional[Alert]:
    """Create an alert and ping Telegram. Same dedupe_key within `dedupe_hours` (unresolved) is skipped."""
    if dedupe_key:
        since = utcnow() - timedelta(hours=dedupe_hours)
        dup = (db.query(Alert).filter(Alert.dedupe_key == dedupe_key, Alert.resolved.is_(False),
                                      Alert.created_at >= since).first())
        if dup:
            return None
    a = Alert(level=level, source=source, message=message[:4000], order_id=order_id, dedupe_key=dedupe_key)
    db.add(a)
    db.flush()
    sim_order = False
    if order_id:
        o = db.get(Order, order_id)
        sim_order = bool(o and o.store_order_id.startswith("SIM-"))
    if level in {"error", "warning"} and not sim_order:  # demo-world problems stay on screen; they do not ping your phone
        tag = "ERROR" if level == "error" else "WARNING"
        link = f"\n{public_url}/alerts" if public_url else ""
        notifier.send(f"[Cheape Deal {tag}] {source}\n{message}{link}")
    return a
