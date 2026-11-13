"""Email page: connect your own mailbox, send mails to customers or companies, let the AI draft (or send) them."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from ..models import EmailLog
from ..security import require_login, verify_csrf
from ..services import assistant as AI
from ..services import mailer
from ..services import settings_service as S
from .deps import get_db
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])
_recent: dict[str, list[float]] = {}
MAX_RECIPIENTS = 20

PRESETS = [("Gmail", "smtp.gmail.com", "587", "Google Account > Security > 2-Step Verification ON karo, phir 'App passwords' se 16 akshar ka password banao. Wahi password yahan daalo (normal password nahi chalega)."),
           ("Outlook / Hotmail", "smtp-mail.outlook.com", "587", "Account ka password ya app password. Kuch accounts mein SMTP pehle settings se on karna padta hai."),
           ("Zoho", "smtp.zoho.in", "587", "Zoho Mail > Settings > Security > App Passwords. (.com account ho to smtp.zoho.com)."),
           ("Apni domain ka mail", "mail.tumhara-domain.com", "587", "Hosting provider (cPanel etc.) ke 'Email accounts' mein SMTP server, port aur password milta hai.")]


def _limited(who: str, per_minute: int = 10) -> bool:
    now = time.monotonic()
    hits = [t for t in _recent.get(who, []) if now - t < 60]
    hits.append(now)
    _recent[who] = hits
    return len(hits) > per_minute


def _split(to: str) -> list[str]:
    return [t.strip() for t in to.replace(";", ",").replace("\n", ",").split(",") if t.strip()]


@router.get("/email", response_class=HTMLResponse)
def email_page(request: Request, db: Session = Depends(get_db)):
    s = mailer.smtp_settings(db)
    logs = db.query(EmailLog).order_by(EmailLog.id.desc()).limit(30).all()
    return page(request, db, "email.html", smtp={k: s[k] for k in ("host", "port", "user", "from", "name", "source", "password_unreadable")}, has_pw=bool(s["password"]),
                ready=mailer.configured(db), logs=logs, presets=PRESETS, ai_on=AI.configured(db), ai_auto=S.get_bool(db, "ai_email_auto"),
                cap=mailer.daily_cap(db), today=mailer.today_count(db), customer_emails=S.get_bool(db, "customer_emails"))


@router.post("/email/smtp", dependencies=[Depends(verify_csrf)])
async def email_save(request: Request, db: Session = Depends(get_db)):
    f = {k: str(v) for k, v in (await request.form()).items()}
    if f.get("action") == "remove":
        mailer.clear_smtp(db)
        db.commit()
        flash(request, "ok", "Mailbox hata diya.")
        return back("/email")
    errs = mailer.save_smtp(db, f.get("host", ""), f.get("port", ""), f.get("user", ""), f.get("password", ""), f.get("from_addr", ""), f.get("name", ""))
    if errs:
        flash(request, "error", " ".join(errs))
        return back("/email")
    S.set_value(db, "email_daily_cap", str(max(0, int(f.get("cap", "30") or 30))) if (f.get("cap", "30") or "30").isdigit() else "30")
    S.set_value(db, "ai_email_auto", "true" if f.get("ai_email_auto") in ("on", "true", "1") else "false")
    db.commit()
    flash(request, "ok", "Mailbox save ho gaya. Neeche 'Test mail' bhejke check karo.")
    return back("/email")


@router.post("/email/test", dependencies=[Depends(verify_csrf)])
async def email_test(request: Request, db: Session = Depends(get_db)):
    to = str((await request.form()).get("to", "")).strip() or mailer.smtp_settings(db)["from"]
    ok, why = mailer.send_logged(db, to, "Cheape Deal test mail", "Ye Cheape Deal ka test mail hai. Agar tumhe ye mila to tumhara mailbox sahi connect hai.", "you")
    db.commit()
    flash(request, "ok" if ok else "error", f"Test mail {to} par gaya." if ok else f"Mail nahi gaya: {_hint(why)}")
    return back("/email")


def _hint(why: str) -> str:
    low = why.lower()
    if "535" in low or "authentication" in low or "username and password" in low:
        return why + " -> Username/password galat hai. Gmail ho to App Password use karo."
    if "timed out" in low or "refused" in low or "unreachable" in low or "name or service" in low:
        return why + " -> Server ka naam ya port galat hai, ya hosting ne outgoing SMTP band rakha hai."
    if "not set up" in low:
        return "Pehle mailbox save karo."
    return why


@router.post("/email/send", dependencies=[Depends(verify_csrf)])
async def email_send(request: Request, db: Session = Depends(get_db)):
    f = {k: str(v) for k, v in (await request.form()).items()}
    tos = _split(f.get("to", ""))
    if not tos or len(tos) > MAX_RECIPIENTS:
        flash(request, "error", f"1 se {MAX_RECIPIENTS} tak email address daalo (comma se alag).")
        return back("/email")
    by = "ai" if f.get("by") == "ai" else "you"
    sent, bad = 0, []
    for t in tos:
        ok, why = mailer.send_logged(db, t, f.get("subject", ""), f.get("body", ""), by)
        if ok:
            sent += 1
        else:
            bad.append(f"{t}: {_hint(why)}")
            if "limit" in why or "Subject" in why:
                break
    db.commit()
    if sent:
        flash(request, "ok", f"{sent} email gaye.")
    if bad:
        flash(request, "error", " | ".join(bad)[:400])
    return back("/email")


@router.post("/email/draft", dependencies=[Depends(verify_csrf)])
async def email_draft(request: Request, db: Session = Depends(get_db)):
    """AI writes subject + body into the form. Nothing is sent."""
    if _limited(request.session.get("user", "?")):
        return JSONResponse({"ok": False, "error": "Thoda ruko: ek minute mein 10 se zyada draft nahi."}, status_code=429)
    try:
        b = await request.json()
    except ValueError:
        b = {}
    ok, res = mailer.draft_with_ai(db, str(b.get("to", ""))[:320], str(b.get("request", "")))
    return JSONResponse({"ok": True, **res} if ok else {"ok": False, "error": res})


@router.post("/email/ai-send", dependencies=[Depends(verify_csrf)])
async def email_ai_send(request: Request, db: Session = Depends(get_db)):
    """AI writes AND sends. Only when the admin switched 'AI can send without my review' on. Same daily cap."""
    if not S.get_bool(db, "ai_email_auto"):
        return JSONResponse({"ok": False, "error": "AI auto-send band hai. Email page par 'AI bina review ke bhej sake' on karo."}, status_code=403)
    if _limited(request.session.get("user", "?"), 5):
        return JSONResponse({"ok": False, "error": "Thoda ruko: ek minute mein 5 se zyada AI email nahi."}, status_code=429)
    try:
        b = await request.json()
    except ValueError:
        b = {}
    tos = _split(str(b.get("to", "")))
    if not tos or len(tos) > MAX_RECIPIENTS:
        return JSONResponse({"ok": False, "error": f"1 se {MAX_RECIPIENTS} tak email address do."})
    ok, res = mailer.draft_with_ai(db, ", ".join(tos), str(b.get("request", "")))
    if not ok:
        return JSONResponse({"ok": False, "error": res})
    sent, bad = 0, []
    for t in tos:
        good, why = mailer.send_logged(db, t, res["subject"], res["body"], "ai")
        sent += good
        if not good:
            bad.append(f"{t}: {_hint(why)}")
    db.commit()
    return JSONResponse({"ok": sent > 0, "sent": sent, "subject": res["subject"], "error": " | ".join(bad)[:300]})
