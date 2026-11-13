"""One page for every key: CJ (supplier), Vercel (frontend), Render (backend), GitHub. Plus the keep-alive ping, and CJ options once CJ is connected."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..models import Connection
from ..security import require_login, verify_csrf
from ..services import bootstrap, keepalive
from ..services import settings_service as S
from .deps import get_db
from .host import cli_ctx
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])


def _cj(db):
    return db.query(Connection).filter(Connection.type == "cj_dropshipping", Connection.enabled.is_(True)).order_by(Connection.id).first()


@router.get("/keys", response_class=HTMLResponse)
def keys_page(request: Request, db: Session = Depends(get_db)):
    cfg = request.app.state.cfg
    cj = _cj(db)
    backend = S.get(db, "host_render_url", "") or cfg.public_url
    site = S.get(db, "host_vercel_url", "") or S.get(db, "shop_site_url", "")
    return page(request, db, "keys.html", cli=cli_ctx(db), cj=cj, cj_ok=bool(cj and cj.last_status == "ok"),
                backend=backend, site=site, last=dict(keepalive.LAST), self_ping=cfg.keepalive_url.startswith("https://"),
                v={k: S.get(db, k, "") for k in ("default_logistic_name", "markup_pct", "min_margin_pct", "from_country_code", "daily_order_cap")})


@router.post("/keys/cj", dependencies=[Depends(verify_csrf)])
async def save_cj(request: Request, db: Session = Depends(get_db)):
    key = str((await request.form()).get("api_key", "")).strip()
    ok, msg = bootstrap.set_cj_key(db, request.app.state.connectors, key)
    if not key:
        flash(request, "error", msg)
    elif ok:
        flash(request, "ok", "CJ key hamesha ke liye save ho gayi (encrypted) aur test pass hua. Neeche CJ ke options aa gaye hain.")
    else:
        flash(request, "error", "CJ key save hui par test fail: " + msg)
    return back("/keys")


@router.post("/keys/cj-config", dependencies=[Depends(verify_csrf)])
async def cj_config(request: Request, db: Session = Depends(get_db)):
    f = {k: str(v).strip() for k, v in (await request.form()).items()}
    errs = []
    try:
        mm, mk = Decimal(f.get("min_margin_pct", "")), Decimal(f.get("markup_pct", ""))
        if not (0 <= mm < 100) or mk < 0:
            raise InvalidOperation
    except InvalidOperation:
        errs.append("Margin (0-99) aur markup (0 ya zyada) number hone chahiye.")
    cc = f.get("from_country_code", "").upper()
    if len(cc) != 2 or not cc.isalpha():
        errs.append("Ship-from country 2 akshar ka code ho (jaise CN).")
    if not f.get("daily_order_cap", "").isdigit():
        errs.append("Daily order cap poora number ho (0 = koi limit nahi).")
    if errs:
        for e in errs:
            flash(request, "error", e)
        return back("/keys")
    for k in ("min_margin_pct", "markup_pct", "daily_order_cap"):
        S.set_value(db, k, f[k])
    S.set_value(db, "from_country_code", cc)
    S.set_value(db, "default_logistic_name", f.get("default_logistic_name") or "AUTO")
    db.commit()
    flash(request, "ok", "CJ ke options save ho gaye.")
    return back("/keys")


@router.post("/keys/ping", dependencies=[Depends(verify_csrf)])
def ping_now(request: Request, db: Session = Depends(get_db)):
    cfg = request.app.state.cfg
    backend = S.get(db, "host_render_url", "") or cfg.public_url
    site = S.get(db, "host_vercel_url", "")
    r = keepalive.ping(backend)
    flash(request, "ok" if r["ok"] else "error", f"Backend {r['url']}: " + (f"jag raha hai ({r['ms']} ms)" if r["ok"] else f"jawab nahi diya ({r['status'] or r['error']}). Free server so raha ho sakta hai: 1 minute baad dobara dabao."))
    if site:
        r2 = keepalive.ping(site, "/api/ping")
        flash(request, "ok" if r2["ok"] else "error", f"Dukaan se backend tak {r2['url']}: " + (f"theek ({r2['ms']} ms)" if r2["ok"] else f"jawab nahi ({r2['status'] or r2['error']})"))
    return back("/keys")
