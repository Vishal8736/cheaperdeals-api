"""System page, backups, read-only API, search, CSV export, bulk product actions, customer tracking page, PWA manifest."""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..adapters.base import StoreError
from ..models import Order, Product, StrategyRec
from ..security import require_login, verify_csrf
from ..services import ops as OPS
from ..services import settings_service as S
from ..services import sync as Y
from ..services.pricing import sell_price
from .deps import get_db, get_rt
from .ui import back, flash, page

router = APIRouter()
auth = APIRouter(dependencies=[Depends(require_login)])


@auth.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db)):
    from ..services import wizard
    st = wizard.steps(db, request.app.state.cfg)
    req = [x for x in st if x["required"]]
    return page(request, db, "setup.html", steps=st, done=sum(1 for x in req if x["done"]), total=len(req), next_step=next((x for x in st if x["required"] and not x["done"]), None))


@auth.get("/guide", response_class=HTMLResponse)
def guide_page(request: Request, db: Session = Depends(get_db)):
    return page(request, db, "guide.html", version=OPS.VERSION)


# ---- system --------------------------------------------------------------------------------
@auth.get("/system", response_class=HTMLResponse)
def system_page(request: Request, db: Session = Depends(get_db)):
    sched = getattr(request.app.state, "scheduler", None)
    alive = None if not request.app.state.cfg.internal_scheduler else bool(sched is not None and sched.is_alive())
    cfg = request.app.state.cfg
    return page(request, db, "system.html", checks=OPS.health_checks(db, cfg, alive), version=OPS.VERSION, key=OPS.api_key(db),
                backups=[(p.name, p.stat().st_size // 1024) for p in OPS.list_backups(cfg)], can_backup=OPS.sqlite_path(cfg) is not None,
                base=str(request.base_url).rstrip("/"), sys={"heartbeat_url": S.get(db, "heartbeat_url", ""), "digest_hour": S.get(db, "digest_hour", "9"),
                "customer_emails": S.get_bool(db, "customer_emails"), "hb_ok": S.get(db, "heartbeat_last_ok", ""), "hb_err": S.get(db, "heartbeat_last_error", "")})


@auth.post("/system/settings", dependencies=[Depends(verify_csrf)])
async def system_settings(request: Request, db: Session = Depends(get_db)):
    form = {k: str(v) for k, v in (await request.form()).items()}
    url = form.get("heartbeat_url", "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        flash(request, "error", "Heartbeat address must start with http:// or https://")
        return back("/system")
    try:
        hour = int(form.get("digest_hour", "9") or 9)
        if not 0 <= hour <= 23:
            raise ValueError
    except ValueError:
        flash(request, "error", "Report hour must be a number from 0 to 23")
        return back("/system")
    S.set_value(db, "heartbeat_url", url)
    S.set_value(db, "digest_hour", str(hour))
    S.set_value(db, "customer_emails", "true" if form.get("customer_emails") in ("on", "true", "1") else "false")
    db.commit()
    flash(request, "ok", "System settings saved.")
    return back("/system")


@auth.post("/system/backup", dependencies=[Depends(verify_csrf)])
def backup_now(request: Request):
    try:
        p = OPS.backup_now(request.app.state.cfg)
        flash(request, "ok" if p else "warn", f"Backup saved: {p.name}" if p else "Backups here work for SQLite only. Use pg_dump for PostgreSQL.")
    except Exception as e:
        flash(request, "error", f"Backup failed: {e}")
    return back("/system")


@auth.get("/system/backup/{name}")
def backup_download(name: str, request: Request):
    for p in OPS.list_backups(request.app.state.cfg):
        if p.name == name:  # only files we made ourselves: no path from the user is ever opened
            return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")
    raise HTTPException(404, "Backup not found")


@auth.post("/system/api-key", dependencies=[Depends(verify_csrf)])
def new_api_key(request: Request, db: Session = Depends(get_db)):
    OPS.api_key(db, rotate=True)
    db.commit()
    flash(request, "ok", "New API key created. The old one stopped working.")
    return back("/system")


# ---- read-only API v1 (X-API-Key) ----------------------------------------------------------
def need_key(x_api_key: str = Header(""), db: Session = Depends(get_db)):
    if not OPS.check_api_key(db, x_api_key):
        raise HTTPException(401, "Missing or wrong X-API-Key")


api = APIRouter(prefix="/api/v1", dependencies=[Depends(need_key)])


@api.get("/products")
def api_products(limit: int = 200, db: Session = Depends(get_db)):
    return [{"id": p.id, "title": p.title, "sku": p.sku, "cost_usd": str(p.supplier_price), "price_usd": str(p.selling_price), "stock": p.stock,
             "active": p.active, "hidden": p.auto_hidden, "status": p.listing_status, "strategy_managed": p.strategy_managed}
            for p in db.query(Product).order_by(Product.id).limit(max(1, min(limit, 1000)))]


@api.get("/orders")
def api_orders(limit: int = 100, status: str = "", db: Session = Depends(get_db)):
    q = db.query(Order).order_by(Order.id.desc())
    if status:
        q = q.filter(Order.status == status)
    return [{"id": o.id, "number": o.store_order_number, "status": o.status, "total": str(o.total), "currency": o.currency, "country": o.ship_country_code,
             "created_at": o.created_at.isoformat(), "tracking": (o.tracking[-1].tracking_number if o.tracking else "")} for o in q.limit(max(1, min(limit, 500)))]


@api.get("/opportunities")
def api_opportunities(db: Session = Depends(get_db)):
    from ..models import Market
    names = {m.id: m.code for m in db.query(Market)}
    return [{"product_id": r.product_id, "market": names.get(r.market_id, ""), "status": r.status, "our_price_usd": str(r.price_usd), "market_price_usd": str(r.reference_usd),
             "profit_usd": str(r.profit_usd), "reason": r.reason} for r in db.query(StrategyRec).order_by(StrategyRec.profit_usd.desc())]


@api.get("/stats")
def api_stats(db: Session = Depends(get_db)):
    from ..services import analytics as AN
    r = AN.report(db, None, 30)
    return {"orders_30d": r["orders"], "revenue_30d": str(r["revenue"]), "profit_30d": str(r["profit"]), "margin_pct": str(r["margin"]), "version": OPS.VERSION}


# ---- search --------------------------------------------------------------------------------
@auth.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", db: Session = Depends(get_db)):
    q = q.strip()[:80]
    prods = orders = []
    if q:
        like = f"%{q}%"
        prods = db.query(Product).filter(or_(Product.title.ilike(like), Product.sku.ilike(like), Product.category.ilike(like))).order_by(Product.id.desc()).limit(25).all()
        orders = db.query(Order).filter(or_(Order.store_order_number.ilike(like), Order.customer_name.ilike(like), Order.customer_email.ilike(like))).order_by(Order.id.desc()).limit(25).all()
    return page(request, db, "search.html", q=q, prods=prods, orders=orders)


# ---- orders CSV ----------------------------------------------------------------------------
@auth.get("/orders/export.csv")
def orders_csv(db: Session = Depends(get_db)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["number", "created_at", "status", "customer", "country", "total", "currency", "tracking", "carrier", "note"])
    for o in db.query(Order).order_by(Order.id).all():
        t = o.tracking[-1] if o.tracking else None
        w.writerow([o.store_order_number, o.created_at.isoformat(timespec="seconds"), o.status, o.customer_name, o.ship_country_code, o.total, o.currency,
                    t.tracking_number if t else "", t.carrier if t else "", o.note])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="cheapedeal-orders.csv"'})


# ---- bulk product actions ------------------------------------------------------------------
@auth.post("/products/bulk", dependencies=[Depends(verify_csrf)])
async def products_bulk(request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("pid") if str(x).isdigit()]
    action, pct = str(form.get("action", "")), str(form.get("pct", "")).strip()
    if not ids or action not in ("activate", "deactivate", "markup"):
        flash(request, "error", "Tick some products and choose an action.")
        return back("/products")
    markup = None
    if action == "markup":
        try:
            markup = Decimal(pct)
            if markup < 0 or markup > 1000:
                raise ValueError
        except Exception:
            flash(request, "error", "Markup % must be a number from 0 to 1000.")
            return back("/products")
    done = failed = 0
    for p in db.query(Product).filter(Product.id.in_(ids)).all():
        try:
            if action == "markup":
                new = sell_price(Decimal(p.supplier_price), markup, S.get(db, "price_ending"))
                if new != Decimal(p.selling_price):
                    Y._push_price(rt, p, new)
                    p.selling_price = new
                p.strategy_managed = False
            else:
                p.active = action == "activate"
                Y._push_visible(rt, p, p.active and not p.auto_hidden)
            done += 1
        except StoreError:
            failed += 1
    db.commit()
    flash(request, "ok" if not failed else "warn", f"{done} product(s) updated" + (f", {failed} could not be changed in the store" if failed else "") + ".")
    return back("/products")


# ---- customer tracking page (no login; the token is the secret) ----------------------------
@router.get("/track/{token}", response_class=HTMLResponse)
def track(token: str, request: Request, db: Session = Depends(get_db)):
    o = db.query(Order).filter(Order.public_token == token).first() if len(token) >= 16 else None
    if o is None:
        raise HTTPException(404, "We could not find that order.")
    steps = [("received", "Order received"), ("submitted_to_supplier", "Sent to the seller"), ("supplier_confirmed", "Seller confirmed"), ("shipped", "Shipped"), ("delivered", "Delivered")]
    order_rank = {s: i for i, (s, _) in enumerate(steps)}
    cur = order_rank.get(o.status, 0 if o.status in ("validated", "pending_approval") else -1)
    last = next((t for t in reversed(o.tracking) if t.tracking_number), None)
    return request.app.state.templates.TemplateResponse(request, "track.html", {"o": o, "steps": steps, "cur": cur, "last": last, "user": None,
                                                                                 "cancelled": o.status == "cancelled"})


@router.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({"name": "Cheape Deal", "short_name": "Cheape Deal", "start_url": "/", "display": "standalone", "background_color": "#F2F2EF",
                         "theme_color": "#6A4CFF", "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]},
                        media_type="application/manifest+json")


# ---- finance, returns, invoice, risk -------------------------------------------------------
from ..models import ReturnCase  # noqa: E402
from ..services import finance as FIN  # noqa: E402
from ..services import risk as RISK  # noqa: E402


@auth.get("/finance", response_class=HTMLResponse)
def finance_page(request: Request, db: Session = Depends(get_db)):
    return page(request, db, "finance.html", r=FIN.pnl(db), b=FIN.business(db))


@auth.post("/finance/business", dependencies=[Depends(verify_csrf)])
async def finance_business(request: Request, db: Session = Depends(get_db)):
    errs = FIN.save_business(db, {k: str(v) for k, v in (await request.form()).items()})
    if errs:
        flash(request, "error", "; ".join(errs))
    else:
        db.commit()
        flash(request, "ok", "Business details saved.")
    return back("/finance")


@auth.get("/finance/export.csv")
def finance_csv(db: Session = Depends(get_db)):
    r = FIN.pnl(db, 120)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["order", "date", "status", "revenue", "refund", "product_cost", "supplier_refund", "shipping", "fees", "net_profit"])
    for x in sorted(r["rows"], key=lambda z: z["order"].id):
        o = x["order"]
        w.writerow([o.store_order_number, o.created_at.date().isoformat(), o.status, x["revenue"], x["refund"], x["cost"], x["supplier_refund"], x["ship"], x["fee"], x["net"]])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="cheapedeal-profit.csv"'})


@auth.post("/orders/{order_id}/ship-cost", dependencies=[Depends(verify_csrf)])
def order_ship_cost(order_id: int, request: Request, cost: str = Form(""), db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if o is None:
        raise HTTPException(404, "Order not found")
    try:
        FIN.set_actual_ship_cost(db, o, cost)
        db.commit()
        flash(request, "ok", "Shipping cost saved.")
    except ValueError as e:
        flash(request, "error", str(e))
    return back(f"/orders/{order_id}")


@auth.get("/orders/{order_id}/invoice", response_class=HTMLResponse)
def order_invoice(order_id: int, request: Request, db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if o is None:
        raise HTTPException(404, "Order not found")
    return request.app.state.templates.TemplateResponse(request, "invoice.html", {"o": o, "inv": FIN.invoice(db, o), "user": request.session.get("user")})


@auth.get("/returns", response_class=HTMLResponse)
def returns_page(request: Request, db: Session = Depends(get_db)):
    return page(request, db, "returns.html", rows=db.query(ReturnCase).order_by(ReturnCase.id.desc()).limit(200).all())


@auth.post("/orders/{order_id}/return", dependencies=[Depends(verify_csrf)])
def return_open(order_id: int, request: Request, reason: str = Form(""), db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if o is None:
        raise HTTPException(404, "Order not found")
    try:
        FIN.open_return(db, o, reason, request.session.get("user", "admin"))
        db.commit()
        flash(request, "ok", "Return opened. Follow it on the Returns page.")
    except ValueError as e:
        db.rollback()
        flash(request, "error", str(e))
    return back("/returns")


@auth.post("/returns/{rid}/{to}", dependencies=[Depends(verify_csrf)])
def return_move(rid: int, to: str, request: Request, refund: str = Form(""), supplier_refund: str = Form(""), note: str = Form(""),
                db: Session = Depends(get_db), rt=Depends(get_rt)):
    r = db.get(ReturnCase, rid)
    if r is None:
        raise HTTPException(404, "Return not found")
    try:
        FIN.move_return(db, rt, r, to, refund=refund, supplier_refund=supplier_refund, note=note, actor=request.session.get("user", "admin"))
        db.commit()
        flash(request, "ok", "Return updated." + (" Remember to refund the customer in your store or payment provider: this page only records it." if to == "refunded" else ""))
    except ValueError as e:
        db.rollback()
        flash(request, "error", str(e))
    return back("/returns")


@auth.get("/risk", response_class=HTMLResponse)
def risk_page(request: Request, db: Session = Depends(get_db)):
    held = db.query(Order).filter(Order.risk_score > 0).order_by(Order.id.desc()).limit(50).all()
    return page(request, db, "risk.html", cfg=RISK.settings(db), held=held)


@auth.post("/risk", dependencies=[Depends(verify_csrf)])
async def risk_save(request: Request, db: Session = Depends(get_db)):
    errs = RISK.save_settings(db, {k: str(v) for k, v in (await request.form()).items()})
    if errs:
        flash(request, "error", "; ".join(errs))
    else:
        db.commit()
        flash(request, "ok", "Risk rules saved.")
    return back("/risk")
