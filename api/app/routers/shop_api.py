"""Public JSON API for the website (frontend on Vercel). No login. Everything here is safe to call from a stranger's browser:
prices come from the database, orders only start the engine after payment, and writes are rate limited per visitor."""
from __future__ import annotations

import hashlib
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..models import ShopOrder, Ticket
from ..services import orders as O
from ..services import payments, shop, support
from .deps import get_db

router = APIRouter(prefix="/shop")
log = logging.getLogger("cheapedeal.shop")
EDGE = "public, max-age=30, s-maxage=300, stale-while-revalidate=3600"  # Vercel's edge answers for 5 min: the server is not woken for browsing


def _who(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or (request.client.host if request.client else "?"))[:64]


def _json(data, request: Request, *, cache: str = "") -> Response:
    body = json.dumps(data, separators=(",", ":"))
    etag = '"' + hashlib.md5(body.encode(), usedforsecurity=False).hexdigest()[:16] + '"'
    headers = {"ETag": etag}
    if cache:
        headers["Cache-Control"] = cache
    if cache and request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


def _process_in_background(app, order_id: int) -> None:
    db = app.state.database.SessionLocal()
    try:
        O.process_order(db, app.state.rt, order_id)
    except Exception:
        log.exception("website order %s: background processing failed (the sweeper will retry)", order_id)
    finally:
        db.close()


@router.get("/config")
def config(request: Request, biz: str = "", db: Session = Depends(get_db)):
    return _json(shop.public_config(db, biz[:12]), request, cache=EDGE)


@router.get("/products")
def products(request: Request, biz: str = "", db: Session = Depends(get_db)):
    return _json({"products": shop.products(db, biz[:12])}, request, cache=EDGE)


@router.get("/design/{token}.png")
def design_png(token: str, db: Session = Depends(get_db)):
    """The print file for a POD design. Printful downloads it from here. The token is random and only ever matches a design made by our own agent."""
    from ..models import Design
    d = db.query(Design).filter(Design.token == token[:32]).first() if token.isalnum() else None
    if d is None:
        raise HTTPException(404)
    return Response(d.png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400", "X-Robots-Tag": "noindex"})


@router.get("/go/{pid:int}")
def go(pid: int, request: Request, db: Session = Depends(get_db)):
    """Send the visitor to the merchant (our affiliate link) and count the click. Only a stored link of a live affiliate product is ever used."""
    from fastapi.responses import RedirectResponse
    from ..services import affiliate
    if shop.rate_limited(_who(request) + ":go", 60):
        raise HTTPException(429, "Thoda ruko")
    url = affiliate.go_url(db, pid)
    if not url:
        raise HTTPException(404, "Ye product ab available nahi hai")
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer-when-downgrade"})


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@router.post("/quote")
async def quote(request: Request, db: Session = Depends(get_db)):
    """Exact amounts for a cart in the customer's currency (the page shows this before the customer pays)."""
    if shop.rate_limited(_who(request) + ":quote", 30):
        return JSONResponse({"error": "Thoda ruko."}, status_code=429)
    b = await _body(request)
    try:
        return shop.quote_public(shop.quote(db, b.get("items"), str(b.get("currency", ""))))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)


@router.post("/orders")
async def create(request: Request, db: Session = Depends(get_db)):
    if shop.rate_limited(_who(request) + ":order", 6):
        return JSONResponse({"error": "Bahut zyada koshish. Ek minute baad dobara try karo."}, status_code=429)
    data = await _body(request)
    if not data:
        return JSONResponse({"error": "Request samajh nahi aayi."}, status_code=400)
    origin = request.headers.get("origin") or ""
    try:
        so, pay = shop.create_order(db, data, redirect_base=origin if origin.startswith("http") else "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except RuntimeError as e:  # the gateway refused: nothing was saved
        db.rollback()
        log.warning("payment setup failed: %s", e)
        return JSONResponse({"error": "Payment shuru nahi ho paya. Dusra tarika chuno ya thodi der baad try karo."}, status_code=502)
    out = {"number": shop.number(so), "token": so.token, "total": str(so.total), "currency": so.currency, "symbol": shop.symbol(so.currency), "method": so.pay_mode, "pay": pay}
    if so.pay_mode in ("manual", "upi"):
        try:
            request.app.state.rt.alert(db, level="info", source="website", message=f"Naya website order {shop.number(so)} ({so.total} {so.currency}, {so.pay_mode}) payment ka intezar kar raha hai."
                                       + " Paisa aate hi Website page par 'Mark paid' dabao.", dedupe_key=f"shop-new-{so.id}")
        except Exception:
            pass
    return JSONResponse(out, status_code=201)


def _finish(request: Request, background: BackgroundTasks, db: Session, so: ShopOrder, payment_id: str) -> bool:
    order, why = shop.mark_paid(db, request.app.state.rt, so, payment_id)
    db.commit()
    if order is not None and why == "ok":
        background.add_task(_process_in_background, request.app, order.id)
    return order is not None


@router.post("/orders/{token}/verify")
async def verify(token: str, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    """The browser reports a finished Razorpay payment. We trust only the HMAC signature, never the browser's word."""
    if shop.rate_limited(_who(request) + ":verify", 12):
        return JSONResponse({"error": "Thoda ruko."}, status_code=429)
    b = await _body(request)
    so = db.query(ShopOrder).filter(ShopOrder.token == token).first()
    if so is None or so.pay_mode != "razorpay" or not so.pay_ref:
        raise HTTPException(status_code=404, detail="Order nahi mila")
    pid, sig = str(b.get("razorpay_payment_id", "")), str(b.get("razorpay_signature", ""))
    if str(b.get("razorpay_order_id", so.pay_ref)) != so.pay_ref or not payments.razorpay_signature_ok(shop.read_secret(db, "razorpay_key_secret_enc"), so.pay_ref, pid, sig):
        return JSONResponse({"error": "Payment verify nahi hui. Agar paisa kat gaya hai to humein batao: order number saath rakho."}, status_code=400)
    return {"ok": _finish(request, background, db, so, pid), "number": shop.number(so)}


@router.post("/orders/{token}/utr")
async def utr(token: str, request: Request, db: Session = Depends(get_db)):
    """UPI: the customer types the bank reference (UTR) so you can match the payment in your bank app faster. It does NOT mark the order paid."""
    if shop.rate_limited(_who(request) + ":utr", 10):
        return JSONResponse({"error": "Thoda ruko."}, status_code=429)
    b = await _body(request)
    ref = "".join(ch for ch in str(b.get("utr", "")) if ch.isalnum())[:30]
    so = db.query(ShopOrder).filter(ShopOrder.token == token, ShopOrder.status == "pending").first()
    if so is None or len(ref) < 6:
        return JSONResponse({"error": "UTR / transaction id sahi daalo (kam se kam 6 akshar)."}, status_code=422)
    pay = json.loads(so.pay_data or "{}")
    pay["utr"] = ref
    so.pay_data = json.dumps(pay)
    try:
        request.app.state.rt.alert(db, level="info", source="website", message=f"{shop.number(so)}: customer ne UTR {ref} bheja. Bank app mein {so.total} {so.currency} dekho, aaya ho to 'Mark paid'.",
                                   dedupe_key=f"shop-utr-{so.id}-{ref}")
    except Exception:
        pass
    return {"ok": True}


@router.post("/razorpay/webhook")
async def razorpay_webhook(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    """Razorpay -> us, when a payment is captured (covers the customer closing the tab). Header X-Razorpay-Signature."""
    raw = await request.body()
    if not payments.webhook_hex_ok(shop.read_secret(db, "razorpay_webhook_secret_enc"), raw, request.headers.get("x-razorpay-signature", "")):
        raise HTTPException(status_code=401, detail="Bad signature")
    try:
        ev = json.loads(raw)
        pay = ev["payload"]["payment"]["entity"]
    except (ValueError, KeyError, TypeError):
        return {"ok": True, "ignored": "not a payment event"}
    if ev.get("event") not in ("payment.captured", "order.paid"):
        return {"ok": True, "ignored": ev.get("event", "")}
    so = db.query(ShopOrder).filter(ShopOrder.pay_ref == str(pay.get("order_id", "")), ShopOrder.pay_mode == "razorpay").first()
    if so is None:
        return {"ok": True, "ignored": "unknown order"}
    if int(pay.get("amount", 0)) != int(so.total * 100):
        log.warning("razorpay amount mismatch for %s", shop.number(so))
        request.app.state.rt.alert(db, level="error", source="website", message=f"{shop.number(so)}: Razorpay ne {pay.get('amount')} diya par order {int(so.total * 100)} ka hai. Order roka gaya, check karo.",
                                   dedupe_key=f"shop-amt-{so.id}")
        return {"ok": False, "ignored": "amount mismatch"}
    _finish(request, background, db, so, str(pay.get("id", "")))
    return {"ok": True}


@router.get("/track")
def track(request: Request, ref: str = "", email: str = "", db: Session = Depends(get_db)):
    if shop.rate_limited(_who(request) + ":track", 20):
        return JSONResponse({"error": "Thoda ruko."}, status_code=429)
    out = shop.track(db, ref, email)
    if out is None:
        return JSONResponse({"error": "Order nahi mila. Order number (W1001) aur wahi email daalo jo order par tha."}, status_code=404)
    if (ref or "").strip() != out.get("token"):
        out.pop("token", None)  # the secret link is only echoed back to someone who already has it
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@router.post("/subscribe")
async def subscribe(request: Request, db: Session = Depends(get_db)):
    if shop.rate_limited(_who(request) + ":sub", 5):
        return JSONResponse({"error": "Thoda ruko."}, status_code=429)
    try:
        b = await request.json()
    except ValueError:
        b = {}
    if not shop.subscribe(db, str(b.get("email", ""))):
        return JSONResponse({"error": "Email sahi nahi lag raha."}, status_code=422)
    return {"ok": True}


# ---- customer support and reviews ---------------------------------------------------------------------
def _limit(request: Request, what: str, n: int):
    if shop.rate_limited(f"{_who(request)}:{what}", n):
        return JSONResponse({"error": "Bahut zyada koshish. Thodi der baad try karo."}, status_code=429)
    return None


@router.post("/tickets")
async def new_ticket(request: Request, db: Session = Depends(get_db)):
    if (bad := _limit(request, "ticket", 5)) is not None:
        return bad
    try:
        t = support.open_ticket(db, request.app.state.rt, await _body(request))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    return JSONResponse({"id": t.id, "token": t.token}, status_code=201)


def _ticket(db: Session, token: str) -> Ticket:
    t = db.query(Ticket).filter(Ticket.token == token).first()
    if t is None:
        raise HTTPException(status_code=404, detail="Ticket nahi mila")
    return t


@router.get("/tickets/{token}")
def read_ticket(token: str, request: Request, db: Session = Depends(get_db)):
    if (bad := _limit(request, "tread", 30)) is not None:
        return bad
    return JSONResponse(support.thread(_ticket(db, token)), headers={"Cache-Control": "no-store"})


@router.post("/tickets/{token}/reply")
async def ticket_reply(token: str, request: Request, db: Session = Depends(get_db)):
    if (bad := _limit(request, "treply", 10)) is not None:
        return bad
    t = _ticket(db, token)
    try:
        support.customer_reply(db, t, str((await _body(request)).get("message", "")))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    return support.thread(t)


@router.get("/reviews/{product_id}")
def reviews(product_id: int, request: Request, db: Session = Depends(get_db)):
    return _json({"reviews": support.approved_reviews(db, product_id)}, request, cache="public, max-age=30, s-maxage=300, stale-while-revalidate=3600")


@router.post("/reviews")
async def new_review(request: Request, db: Session = Depends(get_db)):
    if (bad := _limit(request, "review", 5)) is not None:
        return bad
    try:
        support.add_review(db, request.app.state.rt, await _body(request))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    return JSONResponse({"ok": True, "note": "Shukriya! Review check hone ke baad dikhega."}, status_code=201)
