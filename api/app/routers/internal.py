"""Endpoints called only by n8n (X-Internal-Key). n8n = scheduler + error handler; the rules live in services/."""
from __future__ import annotations

from typing import Optional

from ..version import VERSION
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..security import require_internal_key
from ..services.job_runner import run_job
from ..services.alerts import raise_alert
from .deps import get_db, get_rt

router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal_key)])


def _job(name: str):
    def endpoint(db: Session = Depends(get_db), rt=Depends(get_rt)):
        return run_job(db, rt, name, trigger="n8n")
    return endpoint


for _path, _name in (("process-pending", "process_pending"), ("intake-sweep", "intake_sweep"), ("tracking-sync", "tracking_sync"),
                     ("stock-price-sync", "stock_price_sync"), ("reconcile", "reconcile"), ("strategy-run", "strategy_run"), ("maintenance", "maintenance"),
                     ("price-watch", "price_watch"), ("playbook", "playbook_run"), ("ai-review", "ai_review"), ("heartbeat", "heartbeat"), ("catalog", "catalog_run"), ("affiliate", "affiliate_run")):
    router.add_api_route(f"/jobs/{_path}", _job(_name), methods=["POST"], name=f"job_{_name}")


class AlertIn(BaseModel):
    workflow: str = "n8n"
    message: str
    level: str = "error"
    order_id: Optional[int] = None


@router.post("/alerts")
def post_alert(body: AlertIn, db: Session = Depends(get_db), rt=Depends(get_rt)):
    """n8n's global Error Workflow posts here -> alerts table + Telegram."""
    a = raise_alert(db, rt.notifier, level=body.level if body.level in {"info", "warning", "error"} else "error",
                    source=f"n8n: {body.workflow}", message=body.message, order_id=body.order_id,
                    dedupe_key=f"n8n-{body.workflow}-{body.message[:60]}", dedupe_hours=1, public_url=rt.cfg.public_url)
    return {"ok": True, "created": a is not None}


class PriceIn(BaseModel):
    product: str  # product id or SKU
    market: str   # market code, e.g. US
    price: str    # shelf price in that market's currency, tax included
    url: str = ""


@router.post("/strategy/prices")
def post_market_price(body: PriceIn, db: Session = Depends(get_db)):
    """A scraper / n8n workflow pushes what a product sells for in a market. Treated as untrusted when the gap looks absurd."""
    from fastapi import HTTPException
    from ..models import Market
    from ..services import strategy as ST
    try:
        ST.seed_markets(db)
        p = ST.find_product(db, body.product)
        m = db.query(Market).filter(Market.code == body.market.strip().upper()).first()
        if m is None:
            raise ValueError(f"unknown market '{body.market}'")
        ST.record_price(db, p, m, body.price, url=body.url, source="api")
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return {"ok": True}


class OfferIn(BaseModel):
    title: str
    price: str
    currency: str = "INR"
    source: str = ""
    url: str = ""
    moq: str = "1"
    qty: str = "100"
    weight_kg: str = "0"
    origin: str = "IN"
    seller: str = ""
    email: str = ""
    phone: str = ""
    category: str = ""
    single_ok: bool = False
    target_market: str = ""
    target_price: str = ""




class EngineStartIn(BaseModel):
    live: bool = False
    site_url: str = ""


@router.post("/engine/start")
def engine_start(body: EngineStartIn, request: Request, db: Session = Depends(get_db)):
    """Called by the launcher (the owner's own copy of this app) after it hosts the store: Start the engine here.
    live=true also turns Rehearsal OFF (never the other way round); the shipping method falls back to AUTO."""
    from ..services import engine
    from ..services import settings_service as S
    if body.site_url.startswith("https://"):
        S.set_value(db, "shop_site_url", body.site_url.rstrip("/"))
    engine.start(db, "launcher")
    if body.live:
        if not S.get(db, "default_logistic_name", "").strip():
            S.set_value(db, "default_logistic_name", "AUTO")
        S.set_value(db, "dry_run", "false")
    db.commit()
    sched = getattr(request.app.state, "scheduler", None)
    if sched is not None:
        sched.kick()
    return {"ok": True, "running": engine.running(db), "dry_run": S.get_bool(db, "dry_run")}


@router.get("/engine/status")
def engine_status(db: Session = Depends(get_db)):
    from ..services import engine
    from ..services import settings_service as S
    from ..models import AgentEvent, Product
    from ..services import autocatalog
    c = autocatalog.counts(db)
    err = db.query(AgentEvent).filter(AgentEvent.agent == "catalog", AgentEvent.level == "warn").order_by(AgentEvent.id.desc()).first()
    return {"running": engine.running(db), "dry_run": S.get_bool(db, "dry_run"), "products": db.query(Product).count(), "listed": c["listed"],
            "catalog_error": (err.message[:160] if err and c["listed"] == 0 else ""), "version": VERSION}
