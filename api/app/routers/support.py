"""Admin: customer tickets (reply, close) and review moderation."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..models import Product, Review, ShopOrder, Ticket
from ..security import require_login, verify_csrf
from ..services import support
from .deps import get_db
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/support", response_class=HTMLResponse)
def support_page(request: Request, show: str = "open", t: int = 0, db: Session = Depends(get_db)):
    q = db.query(Ticket)
    if show in ("open", "answered", "closed"):
        q = q.filter(Ticket.status == show)
    tickets = q.order_by(Ticket.updated_at.desc()).limit(60).all()
    cur = db.get(Ticket, t) if t else (tickets[0] if tickets else None)
    order = db.get(ShopOrder, cur.shop_order_id) if cur and cur.shop_order_id else None
    pend = db.query(Review).filter(Review.status == "pending").order_by(Review.id).all()
    titles = {p.id: p.title for p in db.query(Product).filter(Product.id.in_({r.product_id for r in pend} or {0})).all()}
    counts = {s: db.query(Ticket).filter(Ticket.status == s).count() for s in ("open", "answered", "closed")}
    return page(request, db, "support.html", tickets=tickets, cur=cur, order=order, show=show, counts=counts, kinds=support.KINDS, reviews=pend, titles=titles)


@router.post("/support/{tid}/reply", dependencies=[Depends(verify_csrf)])
async def reply(tid: int, request: Request, db: Session = Depends(get_db)):
    t = db.get(Ticket, tid)
    form = await request.form()
    try:
        if t is None:
            raise ValueError("Ticket nahi mila.")
        support.admin_reply(db, t, str(form.get("message", "")))
        db.commit()
        flash(request, "ok", "Jawab save ho gaya. Email connected ho to customer ko chala gaya, warna wo apne ticket link par dekhega.")
    except ValueError as e:
        flash(request, "error", str(e))
    return back(f"/support?t={tid}&show=all")


@router.post("/support/{tid}/status", dependencies=[Depends(verify_csrf)])
async def status(tid: int, request: Request, db: Session = Depends(get_db)):
    t = db.get(Ticket, tid)
    new = str((await request.form()).get("status", ""))
    if t is not None and new in ("open", "closed"):
        t.status = new
        db.commit()
    return back(f"/support?t={tid}&show=all")


@router.post("/support/reviews/{rid}/{action}", dependencies=[Depends(verify_csrf)])
def moderate(rid: int, action: str, request: Request, db: Session = Depends(get_db)):
    r = db.get(Review, rid)
    if r is not None and action in ("approve", "reject"):
        r.status = "approved" if action == "approve" else "rejected"
        db.commit()
        flash(request, "ok", "Review approve ho gaya, dukaan par dikhega." if action == "approve" else "Review hata diya.")
    return back("/support")
