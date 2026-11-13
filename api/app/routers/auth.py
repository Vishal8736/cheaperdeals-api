from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..models import AppUser
from ..security import check_password, csrf_token, totp_ok, verify_csrf
from ..services import secretbox_settings as SB

router = APIRouter()


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return request.app.state.templates.TemplateResponse(request, "login.html", {"error": "", "user": None})


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(""), password: str = Form(""), code: str = Form("")):
    app, ip = request.app, _ip(request)
    tpl = app.state.templates

    def deny(msg: str, status: int = 401):
        return tpl.TemplateResponse(request, "login.html", {"error": msg, "user": None}, status_code=status)
    if app.state.limiter.blocked(ip):
        return deny("Too many failed attempts. Wait 10 minutes and try again.", 429)
    cfg = app.state.cfg
    ok_user = hmac.compare_digest(username.encode(), cfg.admin_username.encode())
    ok_pw = check_password(password, app.state.admin_hash)  # always run bcrypt: no timing hint about the username
    role, who = "", username
    db = app.state.database.SessionLocal()
    try:
        if ok_user and ok_pw:
            role, who = "admin", cfg.admin_username
            secret = SB.totp_secret(db, cfg)
            if secret and not totp_ok(secret, code):
                app.state.limiter.fail(ip)
                return deny("Enter the 6-digit code from your authenticator app." if not code.strip() else "That 2FA code is wrong or expired.")
        elif not ok_user:
            u = db.query(AppUser).filter(AppUser.username == username.strip()).first()
            hashed = u.password_hash if u else app.state.admin_hash
            good = check_password(password, hashed)
            if u and u.enabled and good:
                role, who = u.role, u.username
    finally:
        db.close()
    if role:
        app.state.limiter.reset(ip)
        request.session.clear()
        request.session["user"], request.session["role"] = who, role
        csrf_token(request)
        return RedirectResponse("/", status_code=303)
    app.state.limiter.fail(ip)
    return deny("Wrong username or password.")


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
