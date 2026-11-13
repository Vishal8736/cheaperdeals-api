"""The one Start / Stop switch.

Start = the whole business runs by itself: orders are routed, stock/prices synced, strategy prices applied, cheap offers imported
(inside your capital limit), market prices watched, reports sent. Stop = everything waits; nothing is sent to any supplier.
Rehearsal mode (DRY-RUN) is a SEPARATE safety: Start never turns it off, so you can watch the system work without real orders first.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import settings_service as S
from .agents import event as agent_event
from .alerts import audit

AUTO_ON = {"strategy_autopilot": "true", "strategy_auto_list": "true", "strategy_multi_market": "true",
           "playbook_enabled": "true", "ai_auto": "true"}


def running(db: Session) -> bool:
    return not S.get_bool(db, "paused")


def start(db: Session, actor: str = "admin") -> None:
    S.set_value(db, "paused", "false")
    for k, v in AUTO_ON.items():
        S.set_value(db, k, v)
    S.set_value(db, "engine_started_once", "true")
    audit(db, "system", 0, "engine_started", actor=actor)
    agent_event(db, "reporter", "Start dabaya gaya: saare agents apne aap kaam kar rahe hain", "ok")


def stop(db: Session, actor: str = "admin") -> None:
    S.set_value(db, "paused", "true")
    audit(db, "system", 0, "engine_stopped", actor=actor)
    agent_event(db, "reporter", "Stop dabaya gaya: ab kuch bhi supplier ko nahi jayega", "warn")
