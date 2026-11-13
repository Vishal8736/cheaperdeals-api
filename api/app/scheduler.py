"""Built-in scheduler: runs the same jobs n8n would, inside the app. Used for the Windows/local run so the agents
work with no n8n and no Docker. On the VPS leave it off (INTERNAL_SCHEDULER=false) and let n8n schedule."""
from __future__ import annotations

import logging
import threading
import time

from .services.job_runner import run_job

log = logging.getLogger("cheapedeal.scheduler")

# job -> (every N seconds, first run after N seconds)
SCHEDULE = {
    "process_pending": (20, 3),
    "tracking_sync": (120, 25),
    "intake_sweep": (300, 15),
    "stock_price_sync": (1800, 45),
    "reconcile": (6 * 3600, 90),
    "strategy_run": (3600, 120),
    "maintenance": (3600, 200),
    "price_watch": (12 * 3600, 240),
    "playbook_run": (3600, 100),
    "ai_review": (3600, 180),
    "heartbeat": (300, 30),
    "shop_watch": (120, 40),
}


class Scheduler(threading.Thread):
    def __init__(self, database, rt):
        super().__init__(name="cheapedeal-scheduler", daemon=True)
        self.database, self.rt = database, rt
        self._stop = threading.Event()
        t0 = time.monotonic()
        self._due = {job: t0 + first for job, (_, first) in SCHEDULE.items()}

    def kick(self) -> None:
        """Run every job as soon as possible (used by the Start button)."""
        t0 = time.monotonic()
        for i, job in enumerate(SCHEDULE):
            self._due[job] = t0 + 1 + i

    def stop(self) -> None:
        self._stop.set()

    def run_once(self, job: str) -> None:
        db = self.database.SessionLocal()
        try:
            run_job(db, self.rt, job, trigger="scheduler")
        except Exception:
            log.exception("scheduled job %s failed", job)
        finally:
            db.close()

    def run(self) -> None:
        log.info("built-in scheduler started")
        while not self._stop.wait(2.0):
            now = time.monotonic()
            for job, (every, _) in SCHEDULE.items():
                if now >= self._due[job]:
                    self._due[job] = now + every
                    self.run_once(job)
