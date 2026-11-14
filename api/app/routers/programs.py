"""Programs page: the 30 affiliate / open-market / print-on-demand connectors ranked by the commission they publish, with one-click connect and import."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..connectors.programs import GROUP_LABEL, ranked
from ..models import Connection, Product
from ..security import require_login, verify_csrf
from ..services import affiliate as AF
from ..services import settings_service as S
from .deps import get_db
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])
log = logging.getLogger("cheapedeal.programs")


@router.get("/programs", response_class=HTMLResponse)
def programs_page(request: Request, db: Session = Depends(get_db)):
    conns: dict[str, list[Connection]] = {}
    for c in db.query(Connection).filter(Connection.type.like("aff\\_%", escape="\\") | Connection.type.like("pod\\_%", escape="\\")).order_by(Connection.id).all():
        conns.setdefault(c.type, []).append(c)
    per_conn = dict(db.query(Product.supplier_connection_id, func.count(Product.id)).filter(Product.kind != "dropship").group_by(Product.supplier_connection_id).all())
    clicks = dict(db.query(Product.supplier_connection_id, func.coalesce(func.sum(Product.clicks), 0)).filter(Product.kind == "affiliate").group_by(Product.supplier_connection_id).all())
    top = db.query(Product).filter(Product.kind == "affiliate", Product.clicks > 0).order_by(Product.clicks.desc()).limit(8).all()
    cfg = AF.config(db)
    return page(request, db, "programs.html", programs=ranked(), group_label=GROUP_LABEL, conns=conns, per_conn=per_conn, clicks=clicks, top=top, cfg=cfg, stats=AF.counts(db),
                connected=sum(1 for v in conns.values() for c in v if c.last_status == "ok"))


@router.post("/programs/settings", dependencies=[Depends(verify_csrf)])
async def programs_settings(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    S.set_value(db, "aff_on", "true" if f.get("on") else "false")
    S.set_value(db, "aff_keywords", ", ".join(k.strip() for k in str(f.get("keywords") or "").replace("\n", ",").split(",") if k.strip())[:500])
    for key, lo, hi in (("target", 1, 2000), ("batch", 1, 100)):
        try:
            S.set_value(db, f"aff_{key}", str(max(lo, min(hi, int(float(str(f.get(key) or "0")))))))
        except ValueError:
            pass
    db.commit()
    flash(request, "ok", "Affiliate agent ki setting save ho gayi.")
    return back("/programs")


@router.post("/programs/{cid:int}/import", dependencies=[Depends(verify_csrf)])
async def programs_import(cid: int, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    conn = db.get(Connection, cid)
    if conn is None or conn.kind != "affiliate":
        raise HTTPException(404, "Affiliate connection nahi mili")
    f = await request.form()
    kws = [k.strip() for k in str(f.get("keywords") or "").replace("\n", ",").split(",") if k.strip()][:10]
    try:
        limit = max(1, min(200, int(float(str(f.get("limit") or "20")))))
    except ValueError:
        limit = 20
    database, rt = request.app.state.database, request.app.state.rt

    def go():
        with database.SessionLocal() as s:
            try:
                c = s.get(Connection, cid)
                got = AF.import_now(s, rt, c, kws, limit)
                from ..services.agents import event
                event(s, "affiliate", f"{c.name}: {got['imported']} naye product aaye ({got['duplicate']} pehle se the, {got['no_rate']} currency rate ke bina chhute).", "ok" if got["imported"] else "warn")
            except Exception as e:
                log.warning("affiliate import failed: %s", e)
                from ..services.agents import event
                event(s, "affiliate", f"Import nahi hua: {str(e)[:200]}", "warn")
    background.add_task(go)
    flash(request, "ok", "Import chal pada hai. Thodi der mein Live Agents (Affiliate) aur Products page par naye products dikhenge.")
    return back("/programs")
