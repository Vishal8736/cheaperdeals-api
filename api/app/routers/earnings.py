"""Earnings page: what the networks' APIs say we earned (never estimated)."""
from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import Connection, Earning, Product
from ..security import require_login, verify_csrf
from ..services import earnings as EA
from ..services import settings_service as S
from .deps import get_db
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])
log = logging.getLogger("cheapedeal.earnings")


@router.get("/earnings", response_class=HTMLResponse)
def earnings_page(request: Request, db: Session = Depends(get_db)):
    rt = request.app.state.rt
    conns = db.query(Connection).filter(Connection.type.in_(list(EA.NETWORKS))).order_by(Connection.id).all()
    counts = dict(db.query(Earning.network, func.count(Earning.id)).group_by(Earning.network).all())
    networks = []
    for t, label in EA.NETWORKS.items():
        c = next((x for x in conns if x.type == t), None)
        last = S.get(db, f"earn_last_{c.id}", "")[:16].replace("T", " ") if c else ""
        networks.append({"label": label, "conn": c.name if c else "", "cid": c.id if c else 0, "last": last, "count": counts.get(t, 0),
                         "ready": bool(c and any(rt.connectors.secrets_of(c).get(k) for k in EA._cred_keys(t)))})
    by_product = db.query(Earning.product_id, func.coalesce(func.sum(Earning.amount_usd), 0)).filter(Earning.product_id.isnot(None), Earning.status != "declined").group_by(Earning.product_id).all()
    names = {p.id: p.title for p in db.query(Product).filter(Product.id.in_([b[0] for b in by_product] or [0])).all()}
    top = sorted(((names.get(pid, f"Product #{pid}"), Decimal(str(v))) for pid, v in by_product), key=lambda x: -x[1])[:8]
    return page(request, db, "earnings.html", t=EA.totals(db), clicks=EA.click_stats(db), networks=networks, names=EA.NETWORKS, top=top,
                rows=db.query(Earning).order_by(Earning.occurred_at.desc()).limit(100).all())


@router.post("/earnings/sync", dependencies=[Depends(verify_csrf)])
def earnings_sync(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    database, rt = request.app.state.database, request.app.state.rt

    def go():
        from ..services.job_runner import run_job
        with database.SessionLocal() as s:
            try:
                run_job(s, rt, "earnings_run", trigger="manual")
            except Exception:
                log.exception("earnings sync failed")
    background.add_task(go)
    flash(request, "ok", "Earnings sync shuru ho gaya. Kuch second baad page refresh karo (Agents page par bhi dikhega).")
    return back("/earnings")
