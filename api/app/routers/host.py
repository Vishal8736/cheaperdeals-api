"""Admin: log in to Vercel / Render / GitHub / Neon through their CLIs, run the launcher, choose Live or Rehearsal."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..security import require_login, verify_csrf
from ..services import hostcli, launch
from ..version import VERSION
from ..services import settings_service as S
from ..services import shopcfg as C
from .deps import get_db
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])


def cli_ctx(db) -> dict:
    out = {"items": []}
    for w in hostcli.PROVIDERS:
        try:
            st = json.loads(S.get(db, f"cli_status_{w}", "") or "{}")
        except ValueError:
            st = {}
        out["items"].append({"key": w, "label": hostcli.LABEL[w], "status": st, "login": hostcli.login_info(db, w), "install": hostcli.INSTALL[w]})
    out["use"] = S.get_bool(db, "host_use_cli")
    out["busy"] = any(i["login"].get("state") == "running" for i in out["items"])
    return out


@router.get("/website/connect", response_class=HTMLResponse)
def connect_page(request: Request, db: Session = Depends(get_db)):
    g = lambda k: S.get(db, k, "")  # noqa: E731
    return page(request, db, "host.html", cli=cli_ctx(db), cli_next="/website/connect", lstate=launch.state(db), llog=launch.log_of(db), live=S.get_bool(db, "host_live"),
                v={k: g(k) for k in ("host_vercel_url", "host_render_url", "host_repo")}, has_admin=bool(C.read_secret(db, "host_env_admin_enc")),
                on_render=bool(__import__("os").getenv("RENDER")), pre=_pre(db), det=_det(db), rver=S.get(db, "host_remote_version", ""), lver=VERSION)


def _pre(db) -> dict:
    try:
        d = json.loads(S.get(db, "host_preflight", "") or "{}")
        d["when"] = __import__("datetime").datetime.fromtimestamp(float(d.get("ts", 0))).strftime("%d %b %H:%M")
        return d
    except (ValueError, TypeError, OSError):
        return {}


def _det(db) -> dict:
    try:
        d = json.loads(S.get(db, "host_detect", "") or "{}")
        d["when"] = __import__("datetime").datetime.fromtimestamp(float(d.get("ts", 0))).strftime("%d %b %H:%M")
        return d if d.get("items") else {}
    except (ValueError, TypeError, OSError):
        return {}


def _next(f: dict) -> str:
    return f.get("next") if f.get("next") in ("/keys", "/website/connect") else "/website/connect"


@router.post("/website/connect/cli/login", dependencies=[Depends(verify_csrf)])
async def cli_login(request: Request, db: Session = Depends(get_db)):
    f = {k: str(v).strip() for k, v in (await request.form()).items()}
    which = f.get("which", "")
    if which not in hostcli.PROVIDERS:
        return back(_next(f))
    try:
        hostcli.start_login(request.app.state.database.SessionLocal, which, background=request.app.state.cfg.app_env != "test")
        S.set_value(db, "host_use_cli", "true")
        db.commit()
        flash(request, "ok", f"{hostcli.SHORT[which]} login shuru. Browser khulega (ya neeche ka link kholo), wahan allow karo. Phir 'Status check' dabao.")
    except hostcli.CliError as e:
        flash(request, "error", str(e))
    return back(_next(f))


@router.post("/website/connect/cli/status", dependencies=[Depends(verify_csrf)])
async def cli_status(request: Request, db: Session = Depends(get_db)):
    f = {k: str(v).strip() for k, v in (await request.form()).items()}
    for w in hostcli.PROVIDERS:
        st = hostcli.status(w)
        S.set_value(db, f"cli_status_{w}", json.dumps(st))
        if st["logged_in"]:
            S.set_value(db, "host_use_cli", "true")
        msg = f"CLI mein login hai ({st['who']})." if st["logged_in"] else (st["hint"] if not st["installed"] else f"CLI mili par login nahi hai: '{hostcli.SHORT[w]} login (CLI)' dabao.")
        flash(request, "ok" if st["logged_in"] else "error", f"{hostcli.LABEL[w]}: {msg}")
    db.commit()
    return back(_next(f))


@router.post("/website/connect/live", dependencies=[Depends(verify_csrf)])
async def set_live(request: Request, db: Session = Depends(get_db)):
    f = {k: str(v).strip() for k, v in (await request.form()).items()}
    S.set_value(db, "host_live", "true" if f.get("live") == "1" else "false")
    db.commit()
    flash(request, "ok", "Live ON: agli baar Render server asli orders CJ ko bhejega." if f.get("live") == "1" else "Rehearsal mode: asli orders CJ ko nahi jayenge.")
    return back("/website/connect")


@router.post("/website/connect/admin-password", dependencies=[Depends(verify_csrf)])
def show_admin_password(request: Request, db: Session = Depends(get_db)):
    pw = C.read_secret(db, "host_env_admin_enc")
    flash(request, "ok" if pw else "error", (f"Render server ka admin login: user 'admin', password: {pw}  (kahin note kar lo)") if pw else "Abhi Render server banaya hi nahi gaya, isliye password nahi hai.")
    return back("/website/connect")


@router.post("/website/connect/update", dependencies=[Depends(verify_csrf)])
def update_code(request: Request, db: Session = Depends(get_db)):
    if launch.state(db) == "running":
        flash(request, "error", "Abhi ek kaam chal raha hai. Khatam hone ka intezaar karo.")
        return back("/website/connect")
    S.set_value(db, "launch_state", "running")  # the page shows "running" at once; the worker resets the log
    db.commit()
    import threading
    bg = request.app.state.cfg.app_env != "test"
    sf = request.app.state.database.SessionLocal
    if bg:
        threading.Thread(target=launch.update_remote, args=(sf, request.session.get("user", "admin")), daemon=True, name="update-remote").start()
    else:
        launch.update_remote(sf, request.session.get("user", "admin"))
    flash(request, "ok", "Naya code bhejna shuru. Neeche haal live dikhta hai.")
    return back("/website/connect")


@router.post("/website/connect/preflight", dependencies=[Depends(verify_csrf)])
def run_preflight(request: Request, db: Session = Depends(get_db)):
    items = launch.preflight(db)
    bad = [i for i in items if i["ok"] is False]
    flash(request, "ok" if not bad else "error", "Sab theek hai, Start dabao." if not bad else f"{len(bad)} cheez ruk rahi hai: neeche 'Start se pehle jaanch' mein dekho.")
    return back("/website/connect")


@router.post("/website/connect/launch", dependencies=[Depends(verify_csrf)])
def launch_now(request: Request, db: Session = Depends(get_db)):
    launch.begin(request.app.state.database.SessionLocal, request.session.get("user", "admin"), background=request.app.state.cfg.app_env != "test")
    flash(request, "ok", "Check shuru ho gaya. Neeche haal live dikhta hai.")
    return back("/website/connect")
