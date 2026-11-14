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
    "catalog_run": (1800, 20),
    "affiliate_run": (3600, 50),
    "pod_run": (1800, 60),
    "earnings_run": (3600, 80),
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


BACKGROUND = {"catalog_run", "affiliate_run", "earnings_run", "pod_run"}


class Scheduler(threading.Thread):
    def __init__(self, database, rt):
        super().__init__(name="cheapedeal-scheduler", daemon=True)
        self.database, self.rt = database, rt
        self._stop = threading.Event()
        self._busy: set[str] = set()
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

    def _in_background(self, job: str) -> None:
        """Slow jobs (the catalog talks to the supplier for a minute or more) get their own thread so orders are never kept waiting."""
        if job in self._busy:
            return
        self._busy.add(job)

        def go():
            try:
                self.run_once(job)
            finally:
                self._busy.discard(job)
        threading.Thread(target=go, name=f"job-{job}", daemon=True).start()

    def run(self) -> None:
        log.info("built-in scheduler started")
        while not self._stop.wait(2.0):
            now = time.monotonic()
            for job, (every, _) in SCHEDULE.items():
                if now >= self._due[job]:
                    self._due[job] = now + every
                    (self._in_background if job in BACKGROUND else self.run_once)(job)
