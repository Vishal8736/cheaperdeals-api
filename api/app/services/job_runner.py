"""One place that runs a named job and records it as an agent run. Used by the n8n endpoints, the built-in
scheduler and the "Run now" buttons, so the live dashboard sees all of them the same way."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from . import orders as O
from . import settings_service as S
from . import ops as OPS
from . import pricefeed as PF

log = logging.getLogger("cheapedeal.jobs")
from . import playbook as PB
from . import assistant as AI
from . import strategy as ST
from . import sync as Y
from .agents import track

JOB_AGENT = {"process_pending": "router", "intake_sweep": "sweeper", "tracking_sync": "tracking",
             "stock_price_sync": "stock", "reconcile": "reconciler", "strategy_run": "strategist", "maintenance": "reporter", "price_watch": "scout", "heartbeat": "reporter", "playbook_run": "playbook", "ai_review": "advisor", "shop_watch": "shop", "catalog_run": "catalog", "affiliate_run": "affiliate"}


def run_job(db: Session, rt, job: str, trigger: str = "scheduler") -> dict:
    agent = JOB_AGENT[job]
    quiet = job in {"process_pending", "intake_sweep", "tracking_sync", "maintenance", "heartbeat", "ai_review", "shop_watch", "catalog_run", "affiliate_run"}  # run often; only show runs that did something
    with track(db, agent, trigger, quiet_if_idle=quiet) as run:
        if job == "process_pending":
            res = O.process_pending(db, rt)
            n = int(res.get("processed", 0))
            run.done(n, f"Processed {n} order(s)" + (f": {res.get('outcomes')}" if n else "") if not res.get("paused") else "Paused")
            if res.get("paused"):
                run.done(0, "Paused")
        elif job == "intake_sweep":
            if S.get_bool(db, "paused"):
                res = {"paused": True}
            else:
                res = Y.sweep_missed_orders(db, rt, hours=48, ingest=True)
                S.mark_run(db, "intake_sweep", f"{res.get('ingested', 0)} imported")
                run.done(int(res.get("ingested", 0)), f"Checked store: {res.get('missing', 0)} missed, {res.get('ingested', 0)} imported")
        elif job == "tracking_sync":
            res = Y.tracking_sync(db, rt)
            n = int(res.get("changed", 0)) if not res.get("paused") else 0
            run.done(n, f"Checked {res.get('checked', 0)} shipment(s), {n} updated, {res.get('notified', 0)} customer(s) told"
                     + (f", {res['errors']} error(s)" if res.get("errors") else ""))
        elif job == "stock_price_sync":
            res = Y.stock_price_sync(db, rt)
            if res.get("paused"):
                run.done(0, "Paused")
            else:
                run.done(int(res["checked"]), f"Checked {res['checked']} product(s): {res['repriced']} repriced, {res['hidden']} hidden, "
                         f"{res['unhidden']} shown again" + (f", {res['errors']} error(s)" if res["errors"] else ""))
        elif job == "strategy_run":
            res = ST.run(db, rt)
            if res.get("paused"):
                run.done(0, "Paused")
            else:
                n = int(res["applied"]) + int(res["reverted"])
                run.done(n, f"Analysed {res['analysed']} product(s): {res['viable']} good opportunities, {res['applied']} priced, "
                         f"{res['reverted']} reverted" + (f", {res['needs_you']} need you" if res["needs_you"] else "")
                         + (f", {res['errors']} error(s)" if res["errors"] else ""))
        elif job == "playbook_run":
            res = PB.run(db, rt)
            if res.get("off"):
                run.done(0, "Off (press Start or turn strategies on in Playbook)")
            else:
                run.done(res["changes"], f"{res['repriced']} repriced, {res['switched']} supplier switches, {res['retired']} retired, {res['restored']} back to normal")
        elif job == "catalog_run":
            from . import autocatalog as AC
            res = AC.run(db, rt)
            if res.get("paused"):
                run.done(0, "Paused")
            elif res.get("off"):
                run.done(0, "Auto catalog band hai (Catalog page par chalu karo)")
            elif res.get("full"):
                run.done(0, f"Catalog bhara hua: {res['listed']}/{res['target']} products website par")
            else:
                run.done(int(res.get("listed", 0)) + int(res.get("imported", 0)),
                         f"{res.get('imported', 0)} naye product aaye, {res.get('listed', 0)} website par lage, {res.get('drafts', 0)} draft mein ruke"
                         + (f". Rukawat: {res['error']}" if res.get("error") else ""))
        elif job == "affiliate_run":
            from . import affiliate as AF
            res = AF.run(db, rt)
            if res.get("paused") or res.get("none") or res.get("off"):
                run.done(0, "Paused" if res.get("paused") else "Koi affiliate connection nahi (Programs page se jodo)" if res.get("none") else "Band hai (Programs page par chalu karo)")
            else:
                run.done(int(res["imported"]) + int(res["repriced"]) + int(res["hidden"]), f"{res['imported']} naye product, {res['repriced']} ke prices badle, {res['hidden']} feed se hat gaye"
                         + (f". {res['errors']} program mein dikkat (Diagnostics/Agents dekho)" if res["errors"] else ""))
        elif job == "shop_watch":
            from . import shop as SH, supplies
            res = SH.watch(db, rt)
            bal = supplies.cj_balance_check(db, rt)
            run.done(res["expired"], f"Website: {res['expired']} purane unpaid order band" + (f"; {bal}" if bal else ""))
        elif job == "ai_review":
            res = AI.review(db)
            built_in = str(res.get("text", "")).startswith("Built-in")
            run.done(1 if res["state"] == "done" else 0, ("Built-in jaanch ki (AI model nahi jura)" if built_in else "AI ne system dekha aur salah likhi") if res["state"] == "done" else str(res.get("why", res["state"])))
        elif job == "price_watch":
            res = PF.run(db, rt)
            if res.get("paused") or res.get("off"):
                run.done(0, "Paused" if res.get("paused") else "Off (set eBay keys in Strategy and turn it on)")
            else:
                run.done(int(res["updated"]), f"Looked up {res['looked_up']} price(s): {res['updated']} updated, {res['skipped']} skipped"
                         + (f", {res['errors']} error(s)" if res["errors"] else ""))
        elif job == "heartbeat":
            res = OPS.heartbeat(db, rt.cfg)
            run.done(int(bool(res.get("pinged"))), "Heartbeat sent" if res.get("pinged") else "Heartbeat off")
        elif job == "maintenance":
            res = OPS.maintenance(db, rt, rt.cfg)
            n = int(res["report"]) + int(bool(res["backup"]))
            run.done(n, ("Daily report sent. " if res["report"] else "") + (f"Backup {res['backup']}." if res["backup"] else ""))
        elif job == "reconcile":
            res = Y.reconcile(db, rt)
            n = len(res.get("problems", []))
            run.done(n, "Everything matches" if not n else f"Found {n} difference(s) - see Alerts")
        else:
            raise ValueError(job)
    return res
