"""Business switchboard: every business has its own ON/OFF; pick any set and Start/Stop them together."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..security import require_login, verify_csrf
from ..services import business as BZ
from ..services import mode as MODE
from ..services import settings_service as S
from .deps import get_db
from .ui import back, engine_switch, flash, page

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/business", response_class=HTMLResponse)
def business_page(request: Request, db: Session = Depends(get_db)):
    rows = BZ.status(db)
    return page(request, db, "business.html", rows=rows, running=not S.get_bool(db, "paused") and S.get_bool(db, "engine_started_once"))


@router.post("/business/toggle", dependencies=[Depends(verify_csrf)])
def business_toggle(request: Request, key: str = Query(...), on: str = Form("0"), db: Session = Depends(get_db)):
    if key not in BZ.BY_KEY or not BZ.BY_KEY[key].built:
        flash(request, "warn", "Ye business abhi bana nahi hai.")
        return back("/business")
    MODE.set_on(db, key, on == "1")
    db.commit()
    sched = getattr(request.app.state, "scheduler", None)
    if on == "1" and sched is not None:
        sched.kick()
    flash(request, "ok", f"{BZ.BY_KEY[key].label}: {'ON' if on == '1' else 'OFF'}. Baaki business waise hi chalte rahenge.")
    return back("/business")


@router.post("/business/bulk", dependencies=[Depends(verify_csrf)])
def business_bulk(request: Request, action: str = Form(...), keys: list[str] = Form([]), db: Session = Depends(get_db)):
    built = [b.key for b in BZ.BUSINESSES if b.built]
    chosen = built if action in ("start_all", "stop_all") else [k for k in keys if k in built]
    if not chosen:
        flash(request, "warn", "Pehle kam se kam ek business tick karo.")
        return back("/business")
    if action in ("start", "start_all"):
        BZ.start_selected(db, chosen)
        db.commit()
        return engine_switch("start", request, "/business", "", db)  # same Start as the top bar: also checks/updates Render + Vercel
    BZ.stop_selected(db, chosen)
    db.commit()
    flash(request, "warn", "Band kiye: " + ", ".join(BZ.BY_KEY[k].label for k in chosen) + ". Baaki business chalte rahenge.")
    return back("/business")


@router.post("/business/site", dependencies=[Depends(verify_csrf)])
def business_site(request: Request, key: str = Form(...), url: str = Form(""), db: Session = Depends(get_db)):
    err = BZ.set_site(db, key, url) if key in BZ.BY_KEY else "Galat business."
    db.commit()
    flash(request, "bad" if err else "ok", err or "Website ka link save ho gaya.")
    return back("/business")
