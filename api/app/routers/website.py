"""Admin page for the public website: look and rules, payments, website orders, which products are shown."""
from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from ..models import Product, ShopOrder, Subscriber
from ..security import require_login, verify_csrf
from ..services import settings_service as S
from ..services import fx, payments, shop
from .deps import get_db, get_rt
from .shop_api import _process_in_background
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])
TEXT_KEYS = ["shop_name", "shop_tagline", "shop_announce", "shop_pay_note", "shop_support_email", "shop_delivery_days", "shop_address", "shop_site_url"]


@router.get("/website", response_class=HTMLResponse)
def website_page(request: Request, db: Session = Depends(get_db)):
    cfg = request.app.state.cfg
    orders = db.query(ShopOrder).order_by(ShopOrder.id.desc()).limit(40).all()
    base = cfg.public_url.rstrip("/")
    drafts = db.query(Product).filter(Product.listing_status != "listed", Product.selling_price > 0, Product.active.is_(True)).count()
    vals = {k: shop.get(db, k) for k in shop.DEFAULTS}
    on = set(m.strip() for m in vals["shop_methods"].split(","))
    return page(request, db, "website.html", v=vals, orders=orders, base=base, drafts=drafts, live=shop.sellable(db).count(),
                listed=db.query(Product).filter(Product.listing_status == "listed").count(), subs=db.query(Subscriber).count(),
                methods=[(m, payments.LABELS[m], m in on, payments.ready(db, m)) for m in payments.ALL], active=payments.enabled(db),
                rzp=bool(shop.read_secret(db, "razorpay_key_secret_enc")),
                currencies=shop.allowed_currencies(db), cj_balance=S.get(db, "cj_balance_last", ""),
                shop_open=shop.get(db, "shop_on").lower() not in ("false", "0", "no", "off"), self_ping=bool(cfg.keepalive_url.startswith("https://")))


@router.post("/website/settings", dependencies=[Depends(verify_csrf)])
async def save_settings(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    f = {k: str(v).strip() for k, v in form.items()}
    for k in TEXT_KEYS:
        if k in f:
            S.set_value(db, k, f[k][:400])
    for k in ("shop_ship_flat", "shop_free_over", "cj_min_balance"):
        try:
            v = Decimal(f.get(k, "0") or "0")
            if v < 0:
                raise InvalidOperation
            S.set_value(db, k, str(shop.money(v)))
        except InvalidOperation:
            flash(request, "error", "Shipping, free-shipping aur CJ minimum balance sahi number hone chahiye.")
            return back("/website")
    codes = [c.strip().upper() for c in f.get("shop_currencies", "").split(",") if c.strip()]
    if not codes or any(not (len(c) == 3 and c.isalpha()) for c in codes):
        flash(request, "error", "Currencies 3-akshar ke code hone chahiye, comma se alag: USD,INR,EUR")
        return back("/website")
    S.set_value(db, "shop_currencies", ",".join(dict.fromkeys(codes)))
    default = f.get("shop_currency", "USD").upper()[:3]
    S.set_value(db, "shop_currency", default if default in codes else codes[0])
    S.set_value(db, "shop_countries", ",".join(c.strip().upper()[:2] for c in f.get("shop_countries", "").split(",") if c.strip()) or "US")
    S.set_value(db, "shop_on", "true" if f.get("shop_on") else "false")
    for k in ("razorpay_key_id", "upi_vpa", "upi_name"):
        if k in f:
            S.set_value(db, k, f[k][:200])
    for form_key, store_key in (("razorpay_key_secret", "razorpay_key_secret_enc"), ("razorpay_webhook_secret", "razorpay_webhook_secret_enc")):
        if f.get(form_key):
            shop.save_secret(db, store_key, f[form_key])
    chosen = [m for m in payments.ALL if form.get("method_" + m)]
    S.set_value(db, "shop_methods", ",".join(chosen) or "manual")
    shop.drop_cache()
    db.commit()
    notready = [payments.LABELS[m] for m in chosen if not payments.ready(db, m)]
    if notready:
        flash(request, "error", "Ye tarike chune hain par poori jaankari nahi di, isliye abhi band rahenge: " + ", ".join(notready))
    else:
        flash(request, "ok", "Website settings save ho gayi. Dukaan par 5 minute ke andar dikhengi.")
    return back("/website")


@router.post("/website/rates", dependencies=[Depends(verify_csrf)])
def refresh_rates(request: Request, db: Session = Depends(get_db)):
    fx.ensure_seed(db)
    r = fx.refresh(db)
    db.commit()
    shop.drop_cache()
    flash(request, "ok" if r["ok"] else "error", f"{r['updated']} currency ke rate update ho gaye." if r["ok"] else r["error"])
    return back("/website")


@router.post("/website/watch", dependencies=[Depends(verify_csrf)])
def watch_now(request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    from ..services import supplies
    res = shop.watch(db, rt)
    note = supplies.cj_balance_check(db, rt, force=True)
    db.commit()
    flash(request, "ok", f"Check ho gaya: {res['expired']} purane unpaid order band." + (f" {note}." if note else ""))
    return back("/website")


@router.post("/website/orders/{sid}/refund", dependencies=[Depends(verify_csrf)])
def refund_order(sid: int, request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    so = db.get(ShopOrder, sid)
    ok, msg = shop.refund(db, rt, so) if so else (False, "Order nahi mila.")
    db.commit()
    flash(request, "ok" if ok else "error", msg)
    return back("/website")


@router.post("/website/publish", dependencies=[Depends(verify_csrf)])
async def publish(request: Request, db: Session = Depends(get_db)):
    n = 0
    for p in db.query(Product).filter(Product.listing_status != "listed", Product.selling_price > 0, Product.active.is_(True)).all():
        n += 1 if shop.publish_to_website(db, p) else 0
    db.commit()
    flash(request, "ok", f"{n} product website par laga diye." if n else "Koi aisa product nahi mila jiska selling price set ho.")
    return back("/website")


@router.post("/website/orders/{sid}/paid", dependencies=[Depends(verify_csrf)])
def mark_paid(sid: int, request: Request, background: BackgroundTasks, db: Session = Depends(get_db), rt=Depends(get_rt)):
    so = db.get(ShopOrder, sid)
    if so is None:
        flash(request, "error", "Order nahi mila.")
        return back("/website")
    order, why = shop.mark_paid(db, rt, so, "manual:" + str(request.session.get("user", "admin")), by="manual")
    db.commit()
    if order is None:
        flash(request, "error", why)
    else:
        if why == "ok":
            background.add_task(_process_in_background, request.app, order.id)
        flash(request, "ok", f"{shop.number(so)} paid mark ho gaya. Engine ab supplier ko order bhejega.")
    return back("/website")


@router.post("/website/orders/{sid}/cancel", dependencies=[Depends(verify_csrf)])
def cancel(sid: int, request: Request, db: Session = Depends(get_db)):
    so = db.get(ShopOrder, sid)
    flash(request, "ok" if so and shop.cancel(db, so) else "error", "Order cancel kar diya." if so and so.status == "cancelled" else "Sirf payment-pending order cancel ho sakta hai.")
    db.commit()
    return back("/website")


@router.get("/website/subscribers.csv")
def subscribers_csv(db: Session = Depends(get_db)):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["email", "joined"])
    for s in db.query(Subscriber).order_by(Subscriber.id):
        w.writerow([s.email, s.created_at.isoformat()])
    return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=subscribers.csv"})
