"""POD page: make designs, choose product types, push them to Printful/Printify, see what happened."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from ..models import Design, PodItem
from ..security import require_login, verify_csrf
from ..services import pod_agent, pod_designer as D, pod_publish as PP
from ..services import settings_service as S
from .deps import get_db
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])
log = logging.getLogger("cheapedeal.pod")


@router.get("/pod", response_class=HTMLResponse)
def pod_page(request: Request, db: Session = Depends(get_db)):
    designs = db.query(Design).order_by(Design.id.desc()).limit(24).all()
    items: dict[int, list[PodItem]] = {}
    for it in db.query(PodItem).filter(PodItem.design_id.in_([d.id for d in designs] or [0])).order_by(PodItem.id).all():
        items.setdefault(it.design_id, []).append(it)
    return page(request, db, "pod.html", cfg=D.config(db), types=PP.PRODUCT_TYPES, designs=designs, items=items, conns=pod_agent.connections(db), n=PP.counts(db),
                niches=sorted(D.NICHES), prices={k: S.get(db, f"pod_price_{k}", v["price"]) for k, v in PP.PRODUCT_TYPES.items()})


@router.get("/pod/design/{did}.png")
def pod_preview(did: int, db: Session = Depends(get_db)):
    d = db.get(Design, did)
    if d is None:
        raise HTTPException(404)
    return Response(d.png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/pod/settings", dependencies=[Depends(verify_csrf)])
async def pod_settings(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    S.set_value(db, "pod_niches", ",".join(x.strip().lower() for x in str(f.get("niches", "")).split(",") if x.strip())[:300])
    S.set_value(db, "pod_types", ",".join(t for t in f.getlist("types") if t in PP.PRODUCT_TYPES))
    for k, lo, hi, default in (("pod_daily", 1, 30, 4), ("pod_batch", 1, 5, 2)):
        try:
            S.set_value(db, k, str(max(lo, min(hi, int(str(f.get(k, default)) or default)))))
        except ValueError:
            pass
    try:
        S.set_value(db, "pod_markup", str(max(1.2, min(6.0, float(str(f.get("pod_markup", "2.2")))))))
    except ValueError:
        pass
    for k in PP.PRODUCT_TYPES:
        try:
            v = float(str(f.get(f"price_{k}", "")))
            if 1 <= v <= 500:
                S.set_value(db, f"pod_price_{k}", f"{v:.2f}")
        except ValueError:
            pass
    db.commit()
    flash(request, "ok", "POD settings save ho gayi.")
    return back("/pod")


def _bg(request: Request, background: BackgroundTasks, fn) -> None:
    database, rt = request.app.state.database, request.app.state.rt

    def go():
        with database.SessionLocal() as s:
            try:
                fn(s, rt, rt.cfg)
                s.commit()
            except Exception:
                s.rollback()
                log.exception("pod background task failed")
    background.add_task(go)


@router.post("/pod/make", dependencies=[Depends(verify_csrf)])
def pod_make(request: Request, background: BackgroundTasks, niche: str = Form("positive"), text: str = Form(""), push: str = Form(""), db: Session = Depends(get_db)):
    niche = (niche or "positive").strip().lower()[:80]
    try:
        if text.strip():
            d = D.make_design(db, niche, text, "manual")
        else:
            pick = D.slogans(db, niche, 1)
            if not pick:
                flash(request, "warn", f"'{niche}' ke naye slogans nahi mile: apna text likho ya AI model jodo.")
                return back("/pod")
            d = D.make_design(db, niche, pick[0][0], pick[0][1])
    except ValueError as e:
        flash(request, "bad", str(e))
        return back("/pod")
    db.commit()
    if push == "1":
        did = d.id
        _bg(request, background, lambda s, rt, cfg: _publish_all(s, rt, cfg, did))
        flash(request, "ok", f"Design '{d.text}' ban gaya aur supplier ko bheja ja raha hai (kuch minute). Neeche haal dikhega.")
    else:
        flash(request, "ok", f"Design '{d.text}' ban gaya.")
    return back("/pod")


def _publish_all(s: Session, rt, cfg, did: int) -> None:
    d = s.get(Design, did)
    if d is None:
        return
    types = [t for t in D.config(s)["types"] if t in PP.PRODUCT_TYPES]
    for conn in pod_agent.connections(s):
        for t in types:
            PP.publish_item(s, rt, cfg, d, conn, t)
            s.commit()


@router.post("/pod/publish/{did}", dependencies=[Depends(verify_csrf)])
def pod_publish(did: int, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    if db.get(Design, did) is None:
        raise HTTPException(404)
    if not pod_agent.connections(db):
        flash(request, "warn", "Pehle Programs page se Printful ya Printify jodo.")
        return back("/pod")
    _bg(request, background, lambda s, rt, cfg: _publish_all(s, rt, cfg, did))
    flash(request, "ok", "Supplier ko bheja ja raha hai. Kuch minute baad page refresh karo.")
    return back("/pod")


@router.post("/pod/retry", dependencies=[Depends(verify_csrf)])
def pod_retry(request: Request, background: BackgroundTasks):
    _bg(request, background, lambda s, rt, cfg: pod_agent.retry_errors(s, rt, cfg))
    flash(request, "ok", "Galti wale items dobara try ho rahe hain.")
    return back("/pod")
