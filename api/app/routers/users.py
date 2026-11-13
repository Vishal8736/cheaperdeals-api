"""Users page (admin only: see security.ADMIN_ONLY) and 2FA for the admin."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..models import AppUser
from ..security import hash_password, require_login, totp_new_secret, totp_ok, totp_uri, verify_csrf
from ..services import secretbox_settings as SB
from ..services import settings_service as S
from .deps import get_db
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])


def _admin(request: Request) -> None:
    if request.session.get("role", "admin") != "admin":
        raise HTTPException(403, "Only the admin can do this.")


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db)):
    _admin(request)
    cfg = request.app.state.cfg
    pending = SB.pending_secret(db, cfg)
    return page(request, db, "users.html", users=db.query(AppUser).order_by(AppUser.username).all(), totp_on=S.get_bool(db, "totp_enabled"),
                pending=pending, uri=totp_uri(pending, cfg.admin_username) if pending else "", admin=cfg.admin_username)


@router.post("/users", dependencies=[Depends(verify_csrf)])
def add_user(request: Request, db: Session = Depends(get_db), username: str = Form(""), password: str = Form(""), role: str = Form("viewer")):
    _admin(request)
    username = username.strip()
    if not (3 <= len(username) <= 60) or username.lower() == request.app.state.cfg.admin_username.lower():
        flash(request, "error", "Username must be 3-60 characters and different from the admin's.")
    elif len(password) < 8:
        flash(request, "error", "Password must be at least 8 characters.")
    elif role not in ("operator", "viewer"):
        flash(request, "error", "Role must be operator or viewer.")
    elif db.query(AppUser).filter(AppUser.username == username).first():
        flash(request, "error", "That username already exists.")
    else:
        db.add(AppUser(username=username, password_hash=hash_password(password), role=role))
        db.commit()
        flash(request, "ok", f"User {username} added as {role}.")
    return back("/users")


@router.post("/users/2fa/start", dependencies=[Depends(verify_csrf)])
def tfa_start(request: Request, db: Session = Depends(get_db)):
    _admin(request)
    SB.set_pending(db, request.app.state.cfg, totp_new_secret())
    db.commit()
    return back("/users")


@router.post("/users/2fa/confirm", dependencies=[Depends(verify_csrf)])
def tfa_confirm(request: Request, db: Session = Depends(get_db), code: str = Form("")):
    _admin(request)
    cfg = request.app.state.cfg
    sec = SB.pending_secret(db, cfg)
    if sec and totp_ok(sec, code):
        SB.activate(db, cfg, sec)
        db.commit()
        flash(request, "ok", "2FA is ON. From now on login needs the 6-digit code.")
    else:
        flash(request, "error", "Wrong code. Check the phone clock and try again.")
    return back("/users")


@router.post("/users/2fa/off", dependencies=[Depends(verify_csrf)])
def tfa_off(request: Request, db: Session = Depends(get_db), code: str = Form("")):
    _admin(request)
    cfg = request.app.state.cfg
    if totp_ok(SB.totp_secret(db, cfg), code):
        SB.disable(db)
        db.commit()
        flash(request, "ok", "2FA is OFF.")
    else:
        flash(request, "error", "Enter a valid current code to turn 2FA off.")
    return back("/users")


@router.post("/users/{uid}/{action}", dependencies=[Depends(verify_csrf)])
def user_action(uid: int, action: str, request: Request, db: Session = Depends(get_db), password: str = Form("")):
    _admin(request)
    u = db.get(AppUser, uid)
    if not u:
        raise HTTPException(404, "User not found")
    if action == "toggle":
        u.enabled = not u.enabled
    elif action == "delete":
        db.delete(u)
    elif action == "password" and len(password) >= 8:
        u.password_hash = hash_password(password)
    else:
        flash(request, "error", "Password must be at least 8 characters.")
        return back("/users")
    db.commit()
    flash(request, "ok", "Done.")
    return back("/users")
