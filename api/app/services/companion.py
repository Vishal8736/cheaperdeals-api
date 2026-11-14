"""The AI companion's voice of its own: it notices problems and speaks up without being asked, and now and then says something real about the shop.
Everything it says comes from real data (alerts, error log, connections, orders, products): no invented numbers. The browser shows the notices (speech bubble + voice)."""
from __future__ import annotations

import json
import random
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Alert, Connection, DiagEvent, Order, Product
from . import diag
from . import settings_service as S

KEEP = 40
QUIET_MINUTES = 20  # the same problem is announced again only after this long


def _load(db: Session) -> list[dict]:
    try:
        v = json.loads(S.get(db, "ai_notices", "[]") or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def _push(db: Session, notes: list[dict], level: str, text: str, say: str, key: str) -> None:
    now = utcnow()
    for n in notes[-15:]:  # same problem, recently told: stay quiet
        if n.get("key") == key and now.timestamp() - n.get("ts", 0) < QUIET_MINUTES * 60:
            return
    nid = (notes[-1]["id"] if notes else 0) + 1
    notes.append({"id": nid, "ts": now.timestamp(), "level": level, "text": text[:400], "say": say[:220], "key": key})
    del notes[:-KEEP]


def scan(db: Session) -> list[dict]:
    """Look for NEW problems since the last scan and turn each into a friendly notice. Returns the stored list (oldest first)."""
    notes = _load(db)
    cur = {"alert": 0, "diag": 0, **{k: v for k, v in json.loads(S.get(db, "ai_notice_cursor", "{}") or "{}").items() if isinstance(v, int)}}
    if not S.get(db, "ai_notice_cursor", ""):  # first ever scan: do not shout about history, start from now
        cur["alert"] = db.query(func.coalesce(func.max(Alert.id), 0)).scalar() or 0
        cur["diag"] = db.query(func.coalesce(func.max(DiagEvent.id), 0)).scalar() or 0
    for a in db.query(Alert).filter(Alert.id > cur["alert"], Alert.resolved.is_(False)).order_by(Alert.id).limit(10).all():
        cur["alert"] = max(cur["alert"], a.id)
        if a.level in ("error", "warning"):
            _push(db, notes, a.level, f"Dhyan do: {a.message}", f"Ek alert aaya hai. {a.message[:110]}", f"alert:{a.source}:{a.dedupe_key or a.message[:40]}")
    for e in db.query(DiagEvent).filter(DiagEvent.id > cur["diag"], DiagEvent.level.in_(("ERROR", "CRITICAL"))).order_by(DiagEvent.id).limit(10).all():
        cur["diag"] = max(cur["diag"], e.id)
        hint = diag.hint_for(e.message, e.exc_type)
        fix = f" Kya karo: {hint['fix']}" if hint else " Diagnostics page par poori wajah hai."
        _push(db, notes, "error", f"Ek error aaya: {e.message[:160]}.{fix}", f"Ek error aaya hai. {(hint['title'] if hint else e.message)[:110]}", f"diag:{e.fingerprint}")
    for c in db.query(Connection).filter(Connection.enabled.is_(True), Connection.last_status == "error").all():
        _push(db, notes, "error", f"{c.name} se connection nahi ban raha: {c.last_error[:140]}", f"{c.name} connect nahi ho raha. Dekh lo.", f"conn:{c.id}:{c.last_error[:30]}")
    S.set_value(db, "ai_notice_cursor", json.dumps(cur))
    S.set_value(db, "ai_notices", json.dumps(notes))
    db.commit()
    return notes


def since(db: Session, after: int) -> dict:
    notes = scan(db)
    latest = notes[-1]["id"] if notes else 0
    if after < 0:  # a fresh browser: tell it where we are, do not replay history
        return {"latest": latest, "notices": []}
    return {"latest": latest, "notices": [{k: n[k] for k in ("id", "level", "text", "say")} for n in notes if n["id"] > after]}


def chatter(db: Session) -> str:
    """Something short and TRUE about the shop right now, in a friendly voice (used when the person has been quiet for a while)."""
    hour = (utcnow() + timedelta(hours=5, minutes=30)).hour
    hello = "Good morning!" if 5 <= hour < 12 else ("Namaste!" if hour < 17 else ("Good evening!" if hour < 22 else "Abhi tak jaag rahe ho?"))
    products = db.query(Product).filter(Product.listing_status == "listed").count()
    total = db.query(Product).count()
    orders = db.query(Order).count()
    open_alerts = db.query(Alert).filter(Alert.resolved.is_(False)).count()
    live = not S.get_bool(db, "dry_run")
    lines = []
    if S.get_bool(db, "paused"):
        lines.append(f"{hello} Engine abhi ruka hua hai. Start dabao to saare agents kaam par lag jayenge.")
    else:
        lines.append(f"{hello} Sab agents kaam kar rahe hain.")
    if total == 0:
        lines.append("Abhi ek bhi product nahi hai. Catalog page par Auto catalog chalu kar do, main dekhti rahungi.")
    else:
        lines.append(f"Abhi {products} products dukaan par lage hain, aur kul {total} import hue hain.")
    lines.append(f"Ab tak {orders} order aaye hain." if orders else "Abhi tak koi order nahi aaya. Dukaan ka link share karo!")
    if open_alerts:
        lines.append(f"{open_alerts} alert khule hain, ek baar dekh lo.")
    if not live:
        lines.append("Abhi rehearsal mode chalu hai, asli order CJ ko nahi ja rahe.")
    return " ".join(random.sample(lines[:1], 1) + random.sample(lines[1:], min(2, len(lines) - 1)))
