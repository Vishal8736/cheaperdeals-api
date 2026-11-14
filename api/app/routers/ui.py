from __future__ import annotations

import random
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..adapters.base import StoreError
from ..adapters.mock_store import MockStore
from ..adapters.mock_supplier import MockSupplier
from ..db import utcnow
from ..models import (
    Connection, ProductListing,
    ALL_STATES, Alert, Approval, AuditLog, CANCELLED, CONFIRMED, DELIVERED, FAILED_MANUAL, FAILED_RETRY, Order, PENDING_APPROVAL, PIPELINE_STAGES, PriceHistory, Product, SHIPPED, SUBMITTED,
)
from ..security import require_login, verify_csrf
from ..services import ops as OPS
from ..services import diag
from ..services import business as BZ
from ..services import mode as MODE
from ..services import orders as O
from ..services import settings_service as S
from ..services import sync as Y
from ..services import agents as A
from .deps import get_db, get_rt

router = APIRouter(dependencies=[Depends(require_login)])
PAGE_SIZE = 25
ATTENTION = [PENDING_APPROVAL, FAILED_MANUAL, FAILED_RETRY]

# job -> (label, max age in minutes before we call it stale)
JOBS = {
    "process_pending": ("Order processing", 15),
    "intake_sweep": ("Missed-order sweep", 30),
    "tracking_sync": ("Tracking sync", 5 * 60),
    "stock_price_sync": ("Stock and price sync", 26 * 60),
    "reconcile": ("Nightly reconciliation", 36 * 60),
}
AFF_JOBS = {"affiliate_run": ("Affiliate agent (products + links)", 3 * 60), "earnings_run": ("Earnings sync (network APIs)", 3 * 60), "maintenance": ("Daily report and backup", 26 * 60)}


# ---- helpers -------------------------------------------------------------------------------
def flash(request: Request, kind: str, msg: str) -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def page(request: Request, db: Session, name: str, **extra) -> HTMLResponse:
    ctx = {
        "user": request.session.get("user"),
        "paused": S.get_bool(db, "paused"),
        "dry_run": S.get_bool(db, "dry_run"),
        "nav_approvals": db.query(Approval).filter(Approval.status == "pending").count(),
        "nav_failed": db.query(Order).filter(Order.status.in_([FAILED_MANUAL, FAILED_RETRY])).count(),
        "flashes": [] if request.headers.get("x-live") else request.session.pop("flash", []),
        "live": request.headers.get("x-live") == "1",
        "active": name.split(".")[0],
        "dev_tools": _dev_tools_available(request),
        "nav_errors": diag.error_count(db, 24),
        "mode": MODE.current(db),
        "nav_hide": MODE.nav_hide(db),
    }
    ctx.update(extra)
    return request.app.state.templates.TemplateResponse(request, name, ctx)


def back(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _dev_tools_available(request: Request) -> bool:
    rt = request.app.state.rt
    return bool(request.app.state.cfg.enable_dev_tools and isinstance(rt.supplier, MockSupplier) and isinstance(rt.store, MockStore))


def _dec(raw: str, field: str, *, allow_blank: bool = False, minimum: Decimal | None = Decimal("0")) -> Optional[Decimal]:
    raw = (raw or "").strip()
    if raw == "" and allow_blank:
        return None
    try:
        v = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{field} must be a number")
    if minimum is not None and v < minimum:
        raise ValueError(f"{field} cannot be below {minimum}")
    return v


def product_stats(db: Session) -> dict:
    """Counts for the product tiles: how many we have, how many are live, which supplier / store they belong to."""
    total = db.query(Product).count()
    listed = db.query(Product).filter(Product.listing_status == "listed").count()
    draft = db.query(Product).filter(Product.listing_status == "draft").count()
    hidden = db.query(Product).filter(or_(Product.auto_hidden.is_(True), Product.active.is_(False))).count()
    oos = db.query(Product).filter(Product.stock <= 0).count()
    names = {c.id: c.name for c in db.query(Connection).all()}
    by_sup = [((names.get(cid, "Removed connection") if cid else "Built-in supplier"), n)
              for cid, n in db.query(Product.supplier_connection_id, func.count(Product.id)).group_by(Product.supplier_connection_id).all()]
    by_store = [((names.get(cid, "Removed connection") if cid else "Built-in store"), n)
                for cid, n in db.query(ProductListing.connection_id, func.count(ProductListing.id)).filter(ProductListing.status == "listed")
                .group_by(ProductListing.connection_id).all()]
    legacy = db.query(Product).filter(Product.listing_status == "listed", ~Product.listings.any()).count()  # listed before listings existed
    if legacy:
        by_store.append(("Built-in store", legacy)) if not any(n == "Built-in store" for n, _ in by_store) else None
    top = db.query(Product).order_by(Product.id.desc()).limit(6).all()
    return {"total": total, "listed": listed, "draft": draft, "hidden": hidden, "out_of_stock": oos, "by_supplier": by_sup, "by_store": by_store, "latest": top}


def affiliate_dashboard(db: Session, rt=None) -> dict:
    from ..services import affiliate as AF, earnings as EA
    from ..models import Earning
    conns = db.query(Connection).filter(Connection.kind == "affiliate", Connection.enabled.is_(True)).all()
    jobs = []
    now = utcnow()
    for key, (label, max_age) in AFF_JOBS.items():
        ts, summary = S.last_run(db, key)
        jobs.append({"key": key, "label": label, "ts": ts, "summary": summary, "stale": ts is None or (now - ts) > timedelta(minutes=max_age)})
    return {
        "mode": "affiliate", "aff": AF.counts(db), "clicks": EA.click_stats(db), "earn30": EA.totals(db, 30), "earn_all": EA.totals(db), "epc": EA.epc(db),
        "programs_ok": sum(1 for c in conns if c.last_status == "ok"), "programs": len(conns),
        "api_ready": sum(1 for c in conns if c.type in EA.FETCHERS and rt is not None and any(rt.connectors.secrets_of(c).get(k) for k in EA._cred_keys(c.type))),
        "top": db.query(Product).filter(Product.kind == "affiliate", Product.clicks > 0).order_by(Product.clicks.desc()).limit(6).all(),
        "recent_earn": db.query(Earning).order_by(Earning.occurred_at.desc()).limit(8).all(),
        "alerts": db.query(Alert).filter(Alert.resolved.is_(False)).order_by(Alert.id.desc()).limit(5).all(),
        "open_alerts": db.query(Alert).filter(Alert.resolved.is_(False)).count(),
        "jobs": jobs, "checklist": OPS.checklist(db), "agents": A.snapshot(db),
    }


def dashboard_data(db: Session, cfg, rt=None) -> dict:
    d = affiliate_dashboard(db, rt) if MODE.is_affiliate(db) else _dropship_dashboard(db, cfg)
    d["biz"] = [{"key": k, "label": BZ.BY_KEY[k].label, "on": MODE.is_on(db, k), "built": BZ.BY_KEY[k].built} for k in MODE.KEYS]
    return d


def _dropship_dashboard(db: Session, cfg) -> dict:
    start = O._local_day_start_utc(cfg)
    counts = dict(db.query(Order.status, func.count(Order.id)).group_by(Order.status).all())
    today = db.query(Order).filter(Order.created_at >= start).all()
    live_today = [o for o in today if o.status != CANCELLED]
    revenue = sum((Decimal(o.total) for o in live_today), Decimal("0"))
    margin = Decimal("0")
    for o in live_today:
        for it in o.items:
            if it.product is not None:
                margin += (Decimal(it.unit_price) - Decimal(it.product.supplier_price)) * it.quantity
    jobs = []
    now = utcnow()
    for key, (label, max_age) in JOBS.items():
        ts, summary = S.last_run(db, key)
        stale = ts is None or (now - ts) > timedelta(minutes=max_age)
        jobs.append({"key": key, "label": label, "ts": ts, "summary": summary, "stale": stale})
    return {
        "counts": counts,
        "stages": [(s, counts.get(s, 0)) for s in PIPELINE_STAGES if s != PENDING_APPROVAL],
        "attention": [(s, counts.get(s, 0)) for s in ATTENTION],
        "orders_today": len(today), "revenue": revenue, "margin": margin,
        "recent": db.query(Order).order_by(Order.id.desc()).limit(8).all(),
        "alerts": db.query(Alert).filter(Alert.resolved.is_(False)).order_by(Alert.id.desc()).limit(5).all(),
        "open_alerts": db.query(Alert).filter(Alert.resolved.is_(False)).count(),
        "hidden_products": db.query(Product).filter(Product.auto_hidden.is_(True)).count(),
        "products": db.query(Product).count(),
        "jobs": jobs,
        "checklist": OPS.checklist(db),
        "pstats": product_stats(db),
        "conn_count": db.query(Connection).filter(Connection.enabled.is_(True)).count(),
        "agents": A.snapshot(db),
    }


# ---- dashboard -----------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    return page(request, db, "dashboard.html", d=dashboard_data(db, request.app.state.cfg, request.app.state.rt))


@router.get("/partials/dashboard", response_class=HTMLResponse)
def dashboard_partial(request: Request, db: Session = Depends(get_db)):
    return request.app.state.templates.TemplateResponse(
        request, "partials/_dashboard_body.html", {"d": dashboard_data(db, request.app.state.cfg, request.app.state.rt), "mode": MODE.current(db), "paused": S.get_bool(db, "paused"),
                                                    "dry_run": S.get_bool(db, "dry_run")})


# ---- orders --------------------------------------------------------------------------------
def _orders_query(db: Session, q: str, status: str, page_no: int):
    qs = db.query(Order)
    if status == "attention":
        qs = qs.filter(Order.status.in_(ATTENTION))
    elif status in ALL_STATES:
        qs = qs.filter(Order.status == status)
    if q.strip():
        like = f"%{q.strip()}%"
        qs = qs.filter(or_(Order.store_order_number.ilike(like), Order.store_order_id.ilike(like),
                           Order.customer_name.ilike(like), Order.customer_email.ilike(like)))
    total = qs.count()
    rows = qs.order_by(Order.id.desc()).offset((page_no - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return rows, total


@router.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, q: str = "", status: str = "", p: int = 1, db: Session = Depends(get_db)):
    rows, total = _orders_query(db, q, status, max(p, 1))
    return page(request, db, "orders.html", rows=rows, total=total, q=q, status=status, p=max(p, 1), size=PAGE_SIZE, states=ALL_STATES)


@router.get("/orders/table", response_class=HTMLResponse)
def orders_table(request: Request, q: str = "", status: str = "", p: int = 1, db: Session = Depends(get_db)):
    rows, total = _orders_query(db, q, status, max(p, 1))
    return request.app.state.templates.TemplateResponse(
        request, "partials/_orders_table.html", {"rows": rows, "total": total, "q": q, "status": status, "p": max(p, 1), "size": PAGE_SIZE})


@router.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(order_id: int, request: Request, db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Order not found")
    audit = db.query(AuditLog).filter(AuditLog.entity == "order", AuditLog.entity_id == o.id).order_by(AuditLog.id).all()
    so = o.supplier_order
    lines = []
    for it in o.items:
        m = None
        if it.product and Decimal(it.unit_price) > 0:
            m = ((Decimal(it.unit_price) - Decimal(it.product.supplier_price)) / Decimal(it.unit_price) * 100).quantize(Decimal("0.1"))
        lines.append((it, m))
    live = not S.get_bool(db, "dry_run")
    can = {
        "approve": o.status == PENDING_APPROVAL,
        "retry": o.status in {FAILED_MANUAL, FAILED_RETRY} and not (so and so.state == "attempting"),
        "resolve": bool(so and so.state == "attempting"),
        "cancel": o.status not in {CANCELLED, DELIVERED, SHIPPED},
        "redo_live": live and o.status == SUBMITTED and bool(so and so.dry_run),
    }
    if not o.public_token:
        import secrets as _sec
        o.public_token = _sec.token_hex(12)
        db.commit()
    can["seller_update"] = bool(so and not so.dry_run and o.status in {SUBMITTED, CONFIRMED, SHIPPED})
    return page(request, db, "order_detail.html", o=o, so=so, lines=lines, audit=audit, can=can)


@router.post("/orders/{order_id}/{action}", dependencies=[Depends(verify_csrf)])
def order_action(order_id: int, action: str, request: Request, supplier_order_id: str = Form(""), stage: str = Form(""),
                 tracking: str = Form(""), carrier: str = Form(""), note: str = Form(""), db: Session = Depends(get_db), rt=Depends(get_rt)):
    if not db.get(Order, order_id):
        raise HTTPException(404, "Order not found")
    actor = request.session.get("user", "admin")
    if action == "note":
        db.get(Order, order_id).note = note.strip()[:4000]
        db.commit()
        flash(request, "ok", "Note saved.")
        return back(f"/orders/{order_id}")
    if action == "seller-update":
        r = Y.manual_progress(db, rt, order_id, stage, tracking, carrier, actor)
        db.commit()
        flash(request, "ok" if r["ok"] else "error", ("Saved." + (" The customer was told." if r.get("told_customer") else "")) if r["ok"] else r["error"])
        return back(f"/orders/{order_id}")
    if action == "approve":
        res = O.approve(db, rt, order_id, actor)
    elif action == "reject":
        res = O.reject(db, rt, order_id, actor)
    elif action == "retry":
        res = O.retry(db, rt, order_id, actor)
    elif action == "cancel":
        res = O.cancel(db, rt, order_id, actor)
    elif action == "placed":
        if not supplier_order_id.strip():
            flash(request, "error", "Enter the supplier order ID you saw in the supplier dashboard.")
            return back(f"/orders/{order_id}")
        res = O.resolve_ambiguous(db, rt, order_id, supplier_order_id=supplier_order_id, actor=actor)
    elif action == "not-placed":
        res = O.resolve_ambiguous(db, rt, order_id, supplier_order_id=None, actor=actor)
    elif action == "redo-live":
        res = O.redo_live(db, rt, order_id, actor)
    else:
        raise HTTPException(404, "Unknown action")
    db.commit()
    out = res.get("outcome", "")
    msgs = {
        "submitted": ("ok", "Sent to the supplier."), "dry_run_submitted": ("ok", "Rehearsal done. Nothing was sent to the supplier."),
        "cancelled": ("ok", "Order cancelled." + (" It was already placed with the supplier, so cancel it there too." if res.get("supplier_order_placed") else "")),
        "failed_manual": ("error", res.get("error", "Order still needs you.")),
        "retry_scheduled": ("warn", f"Supplier is busy. Retrying in {res.get('in_minutes')} minutes."),
        "needs_approval": ("warn", "Order is waiting for approval."), "paused": ("warn", "Automation is paused. Resume it first."),
        "needs_resolution": ("warn", "First tell Cheape Deal whether the supplier received this order (see the box below)."),
        "still_dry_run": ("warn", "Turn off DRY-RUN in Settings before placing for real."),
        "daily_cap": ("warn", "Daily order cap reached. The order waits until tomorrow or raise the cap."),
        "not_pending": ("warn", "This order is not waiting for approval."), "not_retryable": ("warn", "This order cannot be retried now."),
        "not_cancellable": ("warn", "This order can no longer be cancelled here."),
    }
    kind, msg = msgs.get(out, ("ok" if out else "warn", out or "Done."))
    flash(request, kind, msg)
    return back(f"/orders/{order_id}")


# ---- approvals -----------------------------------------------------------------------------
@router.get("/approvals", response_class=HTMLResponse)
def approvals_page(request: Request, db: Session = Depends(get_db)):
    pending = (db.query(Approval).filter(Approval.status == "pending").order_by(Approval.id).all())
    recent = db.query(Approval).filter(Approval.status != "pending").order_by(Approval.decided_at.desc()).limit(10).all()
    return page(request, db, "approvals.html", pending=pending, recent=recent)


# ---- products ------------------------------------------------------------------------------
@router.get("/products", response_class=HTMLResponse)
def products_page(request: Request, view: str = "all", q: str = "", supplier: str = "", db: Session = Depends(get_db), rt=Depends(get_rt)):
    from .catalog import store_targets
    qs = db.query(Product)
    if view == "listed":
        qs = qs.filter(Product.listing_status == "listed")
    elif view == "draft":
        qs = qs.filter(Product.listing_status == "draft")
    elif view == "hidden":
        qs = qs.filter(or_(Product.auto_hidden.is_(True), Product.active.is_(False)))
    elif view == "oos":
        qs = qs.filter(Product.stock <= 0)
    if supplier.strip().isdigit():
        qs = qs.filter(Product.supplier_connection_id.is_(None) if supplier == "0" else Product.supplier_connection_id == int(supplier))
    if q.strip():
        like = f"%{q.strip()}%"
        qs = qs.filter(or_(Product.title.ilike(like), Product.sku.ilike(like), Product.supplier_vid.ilike(like)))
    prods = qs.order_by(Product.id.desc()).all()
    names = {c.id: c.name for c in db.query(Connection).all()}
    return page(request, db, "products.html", prods=prods, global_min=S.get_decimal(db, "min_margin_pct"), view=view, q=q, supplier=supplier,
                ps=product_stats(db), names=names, stores=store_targets(db, rt),
                suppliers=[(0, "Built-in supplier")] + [(i, n) for i, n in names.items()
                                                       if db.get(Connection, i).kind == "supplier"])


@router.post("/products", dependencies=[Depends(verify_csrf)])
def product_create(request: Request, title: str = Form(""), store_product_id: str = Form(""), sku: str = Form(""),
                   supplier_vid: str = Form(""), supplier_price: str = Form("0"), selling_price: str = Form("0"),
                   stock: str = Form("0"), min_margin_pct: str = Form(""), db: Session = Depends(get_db)):
    try:
        if not title.strip() or not store_product_id.strip():
            raise ValueError("Name and store product ID are required")
        if db.query(Product).filter(Product.store_product_id == store_product_id.strip()).first():
            raise ValueError(f"A product with store ID {store_product_id.strip()} already exists")
        p = Product(title=title.strip(), store_product_id=store_product_id.strip(), sku=sku.strip(), supplier_vid=supplier_vid.strip(),
                    supplier_price=_dec(supplier_price, "Supplier price"), selling_price=_dec(selling_price, "Selling price"),
                    stock=int(_dec(stock, "Stock")), min_margin_pct=_dec(min_margin_pct, "Minimum margin", allow_blank=True))
    except ValueError as e:
        flash(request, "error", str(e))
        return back("/products")
    db.add(p)
    db.commit()
    flash(request, "ok", f"Added {p.title}.")
    return back("/products")


@router.post("/products/sync", dependencies=[Depends(verify_csrf)])
def products_sync(request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    st = Y.stock_price_sync(db, rt)
    db.commit()
    if st.get("paused"):
        flash(request, "warn", "Automation is paused. Resume it to sync.")
    else:
        flash(request, "ok" if not st["errors"] else "warn",
              f"Checked {st['checked']} products: {st['repriced']} repriced, {st['hidden']} hidden, {st['unhidden']} shown again, {st['errors']} errors.")
    return back("/products")


@router.post("/products/{pid}", dependencies=[Depends(verify_csrf)])
def product_update(pid: int, request: Request, supplier_vid: str = Form(""), selling_price: str = Form("0"),
                   min_margin_pct: str = Form(""), manual_override: Optional[str] = Form(None), active: Optional[str] = Form(None),
                   backup_connection: str = Form(""), backup_vid: str = Form(""), db: Session = Depends(get_db), rt=Depends(get_rt)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "Product not found")
    try:
        new_price = _dec(selling_price, "Selling price", minimum=Decimal("0.01"))
        new_min = _dec(min_margin_pct, "Minimum margin", allow_blank=True)
        if new_min is not None and new_min >= 100:
            raise ValueError("Minimum margin must be below 100")
    except ValueError as e:
        flash(request, "error", str(e))
        return back("/products")
    warnings = []
    price_changed = new_price != Decimal(p.selling_price)
    was_active = p.active
    p.backup_vid = backup_vid.strip()
    p.backup_connection_id = int(backup_connection) if (p.backup_vid and backup_connection.strip().isdigit() and int(backup_connection) > 0) else None
    p.supplier_vid, p.min_margin_pct = supplier_vid.strip(), new_min
    p.manual_override, p.active = manual_override is not None, active is not None
    if price_changed:
        try:
            Y._push_price(rt, p, new_price)
            p.selling_price = new_price
        except StoreError as e:
            warnings.append(f"Price not changed in the store: {e}")
    if p.active != was_active and not p.auto_hidden:
        try:
            Y._push_visible(rt, p, p.active)
        except StoreError as e:
            p.active = was_active
            warnings.append(f"Visibility not changed in the store: {e}")
    db.add(PriceHistory(product_id=p.id, supplier_price=p.supplier_price, selling_price=p.selling_price)) if price_changed else None
    db.commit()
    flash(request, "warn" if warnings else "ok", " ".join(warnings) if warnings else f"Saved {p.title}.")
    return back("/products")


# ---- alerts + audit ------------------------------------------------------------------------
@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, tab: str = "alerts", show: str = "open", order: str = "", db: Session = Depends(get_db)):
    if tab == "audit":
        qs = db.query(AuditLog)
        if order.strip().isdigit():
            qs = qs.filter(AuditLog.entity == "order", AuditLog.entity_id == int(order))
        return page(request, db, "alerts.html", tab="audit", logs=qs.order_by(AuditLog.id.desc()).limit(200).all(), order=order,
                    alerts=[], show=show)
    qs = db.query(Alert)
    if show != "all":
        qs = qs.filter(Alert.resolved.is_(False))
    return page(request, db, "alerts.html", tab="alerts", alerts=qs.order_by(Alert.id.desc()).limit(200).all(), show=show, logs=[], order="")


@router.post("/alerts/resolve-all", dependencies=[Depends(verify_csrf)])
def alerts_resolve_all(request: Request, db: Session = Depends(get_db)):
    n = db.query(Alert).filter(Alert.resolved.is_(False)).update({"resolved": True})
    db.commit()
    flash(request, "ok", f"Marked {n} alerts as done.")
    return back("/alerts")


@router.post("/alerts/{alert_id}/resolve", dependencies=[Depends(verify_csrf)])
def alert_resolve(alert_id: int, db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if a:
        a.resolved = True
        db.commit()
    return back("/alerts")


# ---- settings + controls -------------------------------------------------------------------
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    cfg, rt = request.app.state.cfg, request.app.state.rt
    values = {k: S.get(db, k) for k in S.DEFINITIONS}
    return page(request, db, "settings.html", defs=S.DEFINITIONS, values=values, jobs=dashboard_data(db, cfg)["jobs"],
                supplier_name=rt.supplier.name, store_name=rt.store.name, telegram_on=bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
                webhook_url=f"{cfg.public_url.rstrip('/')}/webhooks/woocommerce")


@router.post("/settings", dependencies=[Depends(verify_csrf)])
async def settings_save(request: Request, db: Session = Depends(get_db)):
    form = {k: str(v) for k, v in (await request.form()).items()}
    errors = S.validate_and_save(db, form)
    if errors:
        db.rollback()
        for e in errors:
            flash(request, "error", e)
    else:
        db.commit()
        flash(request, "ok", "Settings saved.")
    return back("/settings")


@router.post("/settings/mode", dependencies=[Depends(verify_csrf)])
def business_mode(request: Request, mode: str = Form("affiliate"), db: Session = Depends(get_db)):
    m = MODE.set_mode(db, mode)
    db.commit()
    flash(request, "ok", "Affiliate mode chalu: dropship agents so gaye." if m == "affiliate" else "Dropship mode chalu: purane agents jaag gaye.")
    return back("/settings")


@router.post("/lang", dependencies=[Depends(verify_csrf)])
def set_language(request: Request, lang: str = Form("hi"), next: str = Form("/")):
    request.session["lang"] = "en" if lang == "en" else "hi"
    return back(next if next.startswith("/") and not next.startswith("//") else "/")


@router.post("/engine/{action}", dependencies=[Depends(verify_csrf)])
def engine_switch(action: str, request: Request, next: str = Form("/"), local: str = Form(""), db: Session = Depends(get_db)):
    from ..services import engine, launch
    local_only = local == "1"  # "only start here, skip the Render/Vercel check"
    if action not in ("start", "stop"):
        raise HTTPException(404, "Unknown action")
    who = request.session.get("user", "admin")
    if action == "start" and launch.wanted(db) and not local_only:
        db_ = request.app.state.database
        launch.begin(db_.SessionLocal, who, background=request.app.state.cfg.app_env != "test")
        flash(request, "ok", "Check shuru: pehle dekh raha hun ki Render (backend) aur Vercel (website) par tumhari dukaan hai ya nahi, na ho to bana dunga. Haal 'Website jodo' page par live dikhta hai.")
        return back("/website/connect")
    if action == "start":
        engine.start(db, who)
        sched = getattr(request.app.state, "scheduler", None)
        if sched is not None:
            sched.kick()
        flash(request, "ok", "Start ho gaya. Agents ab apne aap kaam karenge: kuch dabane ki zarurat nahi." + (" (Rehearsal ON hai: asli orders supplier ko nahi jayenge.)" if S.get_bool(db, "dry_run") else ""))
    else:
        engine.stop(db, who)
        flash(request, "warn", "Stop ho gaya. Ab kuch bhi supplier ko nahi jayega.")
    db.commit()
    return back(next if next.startswith("/") and not next.startswith("//") else "/")


@router.post("/settings/kill", dependencies=[Depends(verify_csrf)])
def kill_switch(request: Request, state: str = Form(...), next: str = Form("/"), db: Session = Depends(get_db)):
    pause = state == "pause"
    S.set_value(db, "paused", "true" if pause else "false")
    from ..services.alerts import audit
    audit(db, "system", 0, "paused" if pause else "resumed", actor=request.session.get("user", "admin"))
    db.commit()
    flash(request, "warn" if pause else "ok", "Automation paused. Nothing will be sent to the supplier." if pause else "Automation resumed.")
    return back(next if next.startswith("/") and not next.startswith("//") else "/")


@router.post("/settings/dry-run", dependencies=[Depends(verify_csrf)])
def dry_run_toggle(request: Request, state: str = Form(...), db: Session = Depends(get_db)):
    from ..services.alerts import audit
    live = state == "live"
    if live and not S.get(db, "default_logistic_name").strip():
        flash(request, "error", "Set the shipping method name first, then go live.")
        return back("/settings")
    S.set_value(db, "dry_run", "false" if live else "true")
    audit(db, "system", 0, "went_live" if live else "dry_run_on", actor=request.session.get("user", "admin"))
    db.commit()
    flash(request, "warn" if live else "ok", "LIVE. New orders are now placed with the supplier." if live else "Rehearsal mode on. Nothing is sent to the supplier.")
    return back("/settings")


@router.post("/settings/test-telegram", dependencies=[Depends(verify_csrf)])
def test_telegram(request: Request, rt=Depends(get_rt)):
    ok = rt.notifier.send("Cheape Deal test message. Alerts are working.")
    flash(request, "ok" if ok else "error", "Test message sent." if ok else "Could not send. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
    return back("/settings")


# ---- dev tools (mock backends only, ENABLE_DEV_TOOLS=true) ----------------------------------
def _require_dev(request: Request) -> None:
    if not _dev_tools_available(request):
        raise HTTPException(404, "Dev tools are off")


DEMO = [("Desk Lamp", "9001", "LAMP-1", "VID-DEMO-1", "10.00", "26.00", 40),
        ("Phone Stand", "9002", "STAND-1", "VID-DEMO-2", "3.20", "9.99", 120),
        ("Travel Mug", "9003", "MUG-1", "VID-DEMO-3", "5.50", "15.00", 15)]


@router.post("/dev/seed", dependencies=[Depends(verify_csrf)])
def dev_seed(request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    _require_dev(request)
    added = 0
    for title, spid, sku, vid, cost, sell, stock in DEMO:
        if not db.query(Product).filter(Product.store_product_id == spid).first():
            db.add(Product(title=title, store_product_id=spid, sku=sku, supplier_vid=vid, supplier_price=Decimal(cost),
                           selling_price=Decimal(sell), stock=stock))
            added += 1
        rt.supplier.set_product(vid, stock, cost)
    db.commit()
    flash(request, "ok", f"Added {added} demo products.")
    return back("/products")


@router.post("/dev/simulate", dependencies=[Depends(verify_csrf)])
def dev_simulate(request: Request, qty: int = Form(1), fail: str = Form(""), db: Session = Depends(get_db), rt=Depends(get_rt)):
    _require_dev(request)
    prods = db.query(Product).all()
    if not prods:
        flash(request, "warn", "Add demo products first.")
        return back("/settings")
    from ..adapters.base import SupplierError
    rt.supplier.fail_next, rt.supplier.lose_response_next = [], 0  # each test order starts clean
    rt.supplier.supports_lookup = fail != "timeout"
    if fail == "429":
        rt.supplier.fail_next = [SupplierError("Rate limited (simulated 429)", retryable=True, status_code=429)]
    elif fail == "timeout":
        rt.supplier.lose_response_next = 1
    p = random.choice(prods)
    oid = str(random.randint(100000, 999999))
    payload = {"id": oid, "number": oid, "status": "processing", "name": random.choice(["Asha Rao", "Ravi Shah", "Meera Iyer"]),
               "email": "customer@example.com", "phone": "+919800000000", "address1": "12 MG Road", "city": "Pune", "state": "MH",
               "zip": "411001", "country_code": "IN", "country": "India", "currency": "USD",
               "total": str(Decimal(p.selling_price) * max(qty, 1)),
               "items": [{"store_product_id": p.store_product_id, "sku": p.sku, "title": p.title, "quantity": max(qty, 1),
                          "unit_price": str(p.selling_price)}]}
    order, _, _ = O.ingest_payload(db, rt, payload, "dev")
    res = O.process_order(db, rt, order.id)
    db.commit()
    flash(request, "ok", f"Test order #{oid} ({p.title} x{max(qty, 1)}): {res.get('outcome')}.")
    return back(f"/orders/{order.id}")


@router.post("/dev/advance", dependencies=[Depends(verify_csrf)])
def dev_advance(request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    """Move every mock supplier order one step (confirmed -> shipped -> delivered), then run the tracking sync."""
    _require_dev(request)
    for oid, o in rt.supplier.orders.items():
        if o["status"] == "confirmed":
            rt.supplier.advance(oid, "shipped", tracking="TRK" + secrets.token_hex(4).upper(), carrier="MockExpress")
        elif o["status"] == "shipped":
            rt.supplier.advance(oid, "delivered")
    st = Y.tracking_sync(db, rt)
    db.commit()
    flash(request, "ok", f"Supplier moved forward. Tracking sync: {st}")
    return back("/orders")
