"""Playbook page: every dropshipping strategy in one place, each with an on/off switch and plain-language help."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..models import AgentEvent, Product
from ..security import require_login, verify_csrf
from ..services import engine, playbook as PB
from ..services import settings_service as S
from ..services import strategy as ST
from .deps import get_db, get_rt
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/playbook", response_class=HTMLResponse)
def playbook_page(request: Request, db: Session = Depends(get_db)):
    c = PB.cfg(db)
    sale = PB.active_sale(db)
    touched = db.query(Product).filter(Product.playbook_tag != "").order_by(Product.title).limit(50).all()
    events = db.query(AgentEvent).filter(AgentEvent.agent == "playbook").order_by(AgentEvent.id.desc()).limit(15).all()
    pr = ST.params(db)
    return page(request, db, "playbook.html", c=c, sale=sale, touched=touched, events=events, running=engine.running(db), strat_auto=pr.autopilot,
                multi=pr.multi_market, price_ending=S.get(db, "price_ending"), markup=S.get(db, "markup_pct"),
                feed=S.get_bool(db, "pricefeed_enabled"))


@router.post("/playbook", dependencies=[Depends(verify_csrf)])
async def playbook_save(request: Request, db: Session = Depends(get_db)):
    form = {k: str(v) for k, v in (await request.form()).items()}
    errs = PB.save(db, form)
    if errs:
        flash(request, "error", " ".join(errs))
    else:
        db.commit()
        flash(request, "ok", "Playbook save ho gaya. Agent ise agle round mein apne aap lagayega.")
    return back("/playbook")


@router.post("/playbook/run", dependencies=[Depends(verify_csrf)])
def playbook_run(request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    res = PB.run(db, rt)
    flash(request, "warn" if res.get("off") else "ok", "Pehle Start dabao." if res.get("off") else f"Playbook chala: {res['changes']} badlav.")
    return back("/playbook")
