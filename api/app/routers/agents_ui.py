from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..security import require_login, verify_csrf
from ..services import agents as A
from ..services.job_runner import JOB_AGENT, run_job
from .deps import get_db, get_rt
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request, db: Session = Depends(get_db)):
    return page(request, db, "agents.html", snap=A.snapshot(db), scheduler_on=bool(getattr(request.app.state, "scheduler", None)),
                jobs=JOB_AGENT)


@router.get("/partials/agents-live", response_class=HTMLResponse)
def agents_live(request: Request, full: int = 0, db: Session = Depends(get_db)):
    return request.app.state.templates.TemplateResponse(request, "partials/_agents_live.html", {"snap": A.snapshot(db), "full": bool(full)})


@router.post("/agents/run/{job}", dependencies=[Depends(verify_csrf)])
def agents_run(job: str, request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    if job not in JOB_AGENT:
        raise HTTPException(404, "Unknown job")
    run_job(db, rt, job, trigger="manual")
    flash(request, "ok", f"{A.AGENTS[JOB_AGENT[job]]['name']} ran. See the live feed.")
    return back("/agents")


@router.post("/agents/run-all", dependencies=[Depends(verify_csrf)])
def agents_run_all(request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    failed = []
    for job in JOB_AGENT:
        try:
            run_job(db, rt, job, trigger="manual")
        except Exception as e:  # one agent's problem must not stop the others (it is logged with a trace)
            logging.getLogger("cheapedeal.jobs").exception("run-all: job %s failed", job)
            db.rollback()
            failed.append(f"{A.AGENTS[JOB_AGENT[job]]['name']} ({type(e).__name__})")
    if failed:
        flash(request, "warn", "Baaki agents chal gaye. Ye ruke: " + ", ".join(failed) + ". Wajah Diagnostics page par hai.")
    else:
        flash(request, "ok", "Har agent ek baar chal gaya.")
    return back("/agents")
