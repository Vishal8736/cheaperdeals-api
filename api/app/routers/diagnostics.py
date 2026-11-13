"""Diagnostics: every error and warning in one place (grouped, with a plain-language hint), health checks and a one-click report."""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from ..models import DiagEvent
from ..security import require_login, verify_csrf
from ..services import diag
from .deps import get_db
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])
log = logging.getLogger("cheapedeal.diag")


def _hours(v: str) -> int:
    try:
        return max(1, min(24 * 14, int(v)))
    except (TypeError, ValueError):
        return 24


@router.get("/diagnostics", response_class=HTMLResponse)
def diagnostics_page(request: Request, db: Session = Depends(get_db)):
    p = request.query_params
    level = p.get("lvl", "")
    cat = p.get("cat", "")
    q = p.get("q", "").strip()
    ref = p.get("ref", "").strip()
    hours = _hours(p.get("h", "24"))
    view = p.get("view", "problems")
    cfg = request.app.state.cfg
    ctx = dict(level=level, cat=cat, q=q, ref=ref, hours=hours, view=view, cats=diag.CATEGORIES, summary=diag.summary(db, hours), health=diag.health_checks(db, cfg),
               file_log=diag._STATE.get("file", ""), auto=p.get("auto") == "1")
    if view == "stream" or ref:
        ctx["rows"] = diag.recent(db, level or None, cat or None, q or None, hours, ref or None, 300)
        ctx["problems"] = []
        ctx["view"] = "stream"
        ctx["hint_of"] = diag.hint_for
    else:
        ctx["problems"] = diag.groups(db, level or "WARNING", cat or None, q or None, hours)
        ctx["rows"] = []
    return page(request, db, "diagnostics.html", **ctx)


@router.get("/diagnostics/download")
def download(request: Request, db: Session = Depends(get_db)):
    text = diag.report_text(db, request.app.state.cfg, _hours(request.query_params.get("h", "24")))
    name = f"cheaperdeals-diagnostics-{datetime.utcnow():%Y%m%d-%H%M}.txt"
    return Response(text, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.post("/diagnostics/test", dependencies=[Depends(verify_csrf)])
async def write_test(request: Request, db: Session = Depends(get_db)):
    log.info("Test: info line from Diagnostics page")
    log.warning("Test: warning line (ignore me)")
    try:
        raise ValueError("Test error: yeh sirf check hai ki logging chal rahi hai")
    except ValueError:
        log.exception("Test: error with traceback")
    diag.flush()
    flash(request, "ok", "3 test lines likhi (info, warning, error). Neeche 'Sab lines' mein dikhengi; Problems mein 'Test error' dikhna chahiye.")
    return back("/diagnostics?view=stream&q=Test")


@router.post("/diagnostics/clear", dependencies=[Depends(verify_csrf)])
async def clear(request: Request, db: Session = Depends(get_db)):
    f = {k: str(v) for k, v in (await request.form()).items()}
    diag.flush()
    if f.get("mode") == "all":
        n = db.query(DiagEvent).delete()
        db.commit()
    else:
        n = diag.prune(db, days=2)
    flash(request, "ok", f"{n} purani log lines hata di.")
    return back("/diagnostics")
