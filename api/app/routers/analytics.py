from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..security import require_login
from ..services import analytics as AN
from .deps import get_db
from .ui import page

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, days: int = 30, db: Session = Depends(get_db)):
    days = days if days in (7, 30, 90) else 30
    return page(request, db, "analytics.html", r=AN.report(db, request.app.state.cfg, days))
