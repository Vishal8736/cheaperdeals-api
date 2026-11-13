"""The agents: named workers whose activity is shown live on the dashboard.

An "agent" is one job of the system (order intake, supplier submission, tracking...). Each time one runs it writes an
AgentRun row and a few AgentEvent lines. Events use their OWN short database session so they appear immediately
(the live page polls every 3 seconds) and survive even if the surrounding work rolls back.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import timedelta
from typing import Iterator

from sqlalchemy import event as sa_event, func
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import AgentEvent, AgentRun

log = logging.getLogger("cheapedeal.agents")

# key -> (name, what it does, how often it should run in minutes (None = on demand), icon letter)
AGENTS: dict[str, dict] = {
    "intake": {"name": "Order Intake", "does": "Receives paid orders from your stores and saves each one exactly once.", "every": None},
    "router": {"name": "Order Router", "does": "Checks each order, applies the margin and approval rules, and places it with the right supplier.", "every": 15},
    "tracking": {"name": "Tracking", "does": "Asks suppliers for tracking numbers and tells your customers.", "every": 300},
    "stock": {"name": "Stock and Price", "does": "Keeps stock and prices in line with each supplier and hides products that would lose money.", "every": 26 * 60},
    "playbook": {"name": "Playbook", "does": "Runs your dropshipping strategies: demand pricing, clearance, sales, cheapest supplier, retiring dead products.", "every": 90},
    "shop": {"name": "Website", "does": "Closes unpaid website orders after 2 days, keeps exchange rates fresh and warns you when the CJ wallet balance is low.", "every": 120},
    "advisor": {"name": "AI Advisor", "does": "Uses your AI model (Base URL + key) to look over the whole system every hour and write short advice in Hinglish. Read-only.", "every": 90},
    "catalog": {"name": "Catalog", "does": "Browses connected suppliers and imports products you choose.", "every": None},
    "lister": {"name": "Listing", "does": "Publishes products on your stores and websites.", "every": None},
    "strategist": {"name": "Strategy", "does": "Finds the country where each product sells for the most, undercuts the local price and keeps a safe profit.", "every": 6 * 60},
    "scout": {"name": "Price Scout", "does": "Looks up what your products sell for on eBay in other countries (free API) and records those prices for the Strategy.", "every": 6 * 60},
    "reporter": {"name": "Report and Backup", "does": "Sends the daily report, saves a database backup and tidies old activity.", "every": 90},
    "sweeper": {"name": "Missed-Order Sweeper", "does": "Re-checks the store for paid orders a webhook may have missed.", "every": 30},
    "reconciler": {"name": "Reconciler", "does": "Every night compares our records with the suppliers and reports differences.", "every": 36 * 60},
}


def _bind(db: Session):
    return db.get_bind()


def _write_events(bind, rows: list[tuple]) -> None:
    try:
        with Session(bind=bind) as s:
            for agent, level, message, order_id in rows:
                s.add(AgentEvent(agent=agent, level=level, message=message[:500], order_id=order_id))
            s.commit()
    except Exception:  # monitoring must never break real work
        log.exception("could not write agent events")


def event(db: Session, agent: str, message: str, level: str = "info", order_id: int | None = None) -> None:
    """Add one line to the live feed. While the caller is mid-transaction the line is held and written right after the
    commit (so it never fights the caller for the database lock, and never shows work that was rolled back)."""
    row = (agent, level, message, order_id)
    if db.in_transaction():
        db.info.setdefault("pending_events", []).append(row)
    else:
        _write_events(_bind(db), [row])


@sa_event.listens_for(Session, "after_commit")
def _flush_after_commit(session: Session) -> None:
    rows = session.info.pop("pending_events", None)
    if rows:
        _write_events(session.get_bind(), rows)


@sa_event.listens_for(Session, "after_rollback")
def _drop_after_rollback(session: Session) -> None:
    session.info.pop("pending_events", None)


class RunHandle:
    def __init__(self, db: Session, run_id: int | None, agent: str):
        self.db, self.run_id, self.agent = db, run_id, agent
        self.processed = 0
        self.summary = ""

    def log(self, message: str, level: str = "info", order_id: int | None = None) -> None:
        event(self.db, self.agent, message, level, order_id)

    def done(self, processed: int, summary: str = "") -> None:
        self.processed, self.summary = processed, summary


@contextmanager
def track(db: Session, agent: str, trigger: str = "scheduler", *, quiet_if_idle: bool = False) -> Iterator[RunHandle]:
    """with track(db, "tracking") as run: ...; run.done(n, "text").  Records a run + a start/finish event.
    quiet_if_idle: frequent jobs that found nothing to do leave no run row (keeps the feed readable)."""
    run_id = None
    try:
        db.commit()  # settle anything pending so the monitoring writes below never wait on us
    except Exception:
        db.rollback()
    try:
        with Session(bind=_bind(db)) as s:
            r = AgentRun(agent=agent, trigger=trigger, status="running")
            s.add(r)
            s.commit()
            run_id = r.id
    except Exception:
        log.exception("could not start agent run")
    h = RunHandle(db, run_id, agent)
    status, err = "ok", ""
    try:
        yield h
    except Exception as e:
        status, err = "error", f"{type(e).__name__}: {e}"
        raise
    finally:
        silent = quiet_if_idle and h.processed == 0 and status == "ok"
        try:
            db.commit() if status == "ok" else db.rollback()
        except Exception:
            db.rollback()
        try:
            with Session(bind=_bind(db)) as s:
                r = s.get(AgentRun, run_id) if run_id else None
                if r is not None:
                    if silent:
                        s.delete(r)
                    else:
                        r.status, r.error, r.finished_at = status, err, utcnow()
                        r.processed, r.summary = h.processed, (h.summary or "")[:500]
                    s.commit()
        except Exception:
            log.exception("could not finish agent run")
        if not silent:
            event(db, agent, (h.summary or "finished") if status == "ok" else f"Failed: {err}", "ok" if status == "ok" else "error")


def snapshot(db: Session, tz_now=None) -> dict:
    """Everything the live panel needs, in one cheap read."""
    now = utcnow()
    day_ago = now - timedelta(hours=24)
    last = {a: r for a, r in ((r.agent, r) for r in db.query(AgentRun).order_by(AgentRun.id).all()[-400:])}
    runs24 = dict(db.query(AgentRun.agent, func.count(AgentRun.id)).filter(AgentRun.started_at >= day_ago).group_by(AgentRun.agent).all())
    done24 = dict(db.query(AgentRun.agent, func.coalesce(func.sum(AgentRun.processed), 0)).filter(AgentRun.started_at >= day_ago).group_by(AgentRun.agent).all())
    errs24 = dict(db.query(AgentRun.agent, func.count(AgentRun.id)).filter(AgentRun.started_at >= day_ago, AgentRun.status == "error").group_by(AgentRun.agent).all())
    recent_events = db.query(AgentEvent).order_by(AgentEvent.id.desc()).limit(300).all()
    last_ev: dict[str, AgentEvent] = {}
    for e in recent_events:
        last_ev.setdefault(e.agent, e)
    cards = []
    for key, a in AGENTS.items():
        r = last.get(key)
        ev = last_ev.get(key)
        if r is None and ev is not None:
            state_hint = "error" if ev.level == "error" else "idle"
        else:
            state_hint = ""
        if r is not None and r.status == "running" and (now - r.started_at) > timedelta(minutes=10):
            state = "stuck"
        elif r is not None and r.status == "running":
            state = "working"
        elif r is not None and r.status == "error":
            state = "error"
        elif r is None and state_hint:
            state = state_hint
        elif r is None:
            state = "waiting"
        else:
            state = "idle"
        newest = ev if (ev is not None and (r is None or ev.created_at >= (r.finished_at or r.started_at))) else None
        cards.append({"key": key, "name": a["name"], "does": a["does"], "state": state, "last": r, "event": ev,
                      "message": (newest.message if newest else ((r.summary or r.error) if r else "")),
                      "when": (newest.created_at if newest else (r.started_at if r else None)),
                      "runs": runs24.get(key, 0), "processed": int(done24.get(key, 0) or 0), "errors": errs24.get(key, 0)})
    feed = db.query(AgentEvent).order_by(AgentEvent.id.desc()).limit(40).all()
    names = {k: v["name"] for k, v in AGENTS.items()}
    return {"cards": cards, "feed": feed, "names": names,
            "working": sum(c["state"] == "working" for c in cards), "errors": sum(c["state"] in {"error", "stuck"} for c in cards),
            "total_runs": sum(runs24.values()), "total_processed": int(sum(done24.values()))}


def prune(db: Session, keep_days: int = 14) -> None:
    cutoff = utcnow() - timedelta(days=keep_days)
    db.query(AgentEvent).filter(AgentEvent.created_at < cutoff).delete()
    db.query(AgentRun).filter(AgentRun.started_at < cutoff).delete()
