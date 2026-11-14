"""AI Assistant page: chat for everyone who can log in; connection settings for the admin only."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from ..security import require_login, verify_csrf
from ..services import assistant as AI
from ..services import settings_service as S
from .deps import get_db
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])
_recent: dict[str, list[float]] = {}


def _limited(who: str, per_minute: int = 20) -> bool:
    now = time.monotonic()
    hits = [t for t in _recent.get(who, []) if now - t < 60]
    hits.append(now)
    _recent[who] = hits
    return len(hits) > per_minute


@router.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request, db: Session = Depends(get_db)):
    c = AI.config(db)
    return page(request, db, "assistant.html", ai=c, brief=S.get(db, "ai_brief", ""), brief_at=S.get(db, "ai_brief_at", ""), ai_on=AI.configured(db), presets=AI.PRESETS, is_admin=request.session.get("role", "admin") == "admin")


@router.post("/assistant/ask", dependencies=[Depends(verify_csrf)])
async def assistant_ask(request: Request, db: Session = Depends(get_db)):
    if _limited(request.session.get("user", "?")):
        return JSONResponse({"answer": "Thoda ruko: ek minute mein 20 se zyada sawal nahi.", "mode": "offline", "note": ""}, status_code=429)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Bad request")
    if not isinstance(body, dict):
        raise HTTPException(400, "Bad request")
    hist = body.get("history") if isinstance(body.get("history"), list) else []
    hist = [m for m in hist if isinstance(m, dict)]
    return JSONResponse(AI.ask(db, hist, str(body.get("message", "")), with_code=request.session.get("role", "admin") == "admin"))


@router.get("/ai/notices")
def ai_notices(request: Request, after: int = -1, db: Session = Depends(get_db)):
    """The companion polls this: new problems it noticed on its own (nothing is replayed to a fresh browser)."""
    from ..services import companion
    return JSONResponse(companion.since(db, after), headers={"Cache-Control": "no-store"})


@router.get("/ai/chatter")
def ai_chatter(request: Request, db: Session = Depends(get_db)):
    from ..services import companion
    return JSONResponse({"line": companion.chatter(db)}, headers={"Cache-Control": "no-store"})


@router.post("/assistant/settings", dependencies=[Depends(verify_csrf)])
def assistant_settings(request: Request, db: Session = Depends(get_db), base_url: str = Form(""), api_key: str = Form(""), model: str = Form(""),
                       style: str = Form("openai"), auto: str = Form("")):
    errs = AI.save_config(db, base_url, api_key, model, style, auto=auto in ("on", "true", "1"))
    if errs:
        flash(request, "error", " ".join(errs))
    else:
        db.commit()
        ok, msg = AI.test(db) if AI.configured(db) else (None, "")
        flash(request, "ok" if ok else ("warn" if ok is False else "ok"), "AI settings save ho gayi." + (f" Test: {msg}" if msg else ""))
    return back("/assistant")


@router.post("/assistant/test", dependencies=[Depends(verify_csrf)])
def assistant_test(request: Request, db: Session = Depends(get_db)):
    ok, msg = AI.test(db)
    flash(request, "ok" if ok else "error", msg)
    return back("/assistant")


@router.post("/assistant/models", dependencies=[Depends(verify_csrf)])
async def assistant_models(request: Request, db: Session = Depends(get_db)):
    """'Fetch models' button: asks the provider which models this key can use. Uses the saved key when the key box is left empty."""
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Bad request")
    if not isinstance(body, dict):
        raise HTTPException(400, "Bad request")
    saved = AI.config(db)
    base = str(body.get("base_url") or saved["base_url"])
    style = str(body.get("style") or saved["style"])
    key = str(body.get("api_key") or "").strip() or saved["key"]
    try:
        models = AI.list_models(base, key, "anthropic" if style == "anthropic" else "openai")
    except AI.AIError as e:
        return JSONResponse({"ok": False, "error": str(e), "models": []})
    return JSONResponse({"ok": True, "error": "", "models": models})


@router.post("/assistant/review", dependencies=[Depends(verify_csrf)])
def assistant_review(request: Request, db: Session = Depends(get_db)):
    res = AI.review(db, force=True)
    flash(request, "ok" if res["state"] == "done" else "error", "AI ne system dekh liya, neeche salah dekho." if res["state"] == "done" else f"AI review nahi hua: {res.get('why', '')}")
    return back("/assistant")
