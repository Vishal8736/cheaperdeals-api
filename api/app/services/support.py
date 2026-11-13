"""Customer support from the website: tickets (order problems, questions, returns, feedback) and verified-buyer product reviews.

Safety: a customer can only see a ticket through its secret token; a review needs a PAID order (order number + the e-mail used) that contains that
product, one review per product per order, and it stays hidden until you approve it. Text is length-limited and shown as plain text.
"""
from __future__ import annotations

import re
import secrets
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Review, ShopOrder, Ticket, TicketMessage
from . import shopcfg as C

KINDS = {"issue": "Order problem", "question": "Question", "return": "Return / refund", "feedback": "Feedback"}
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


def _clean(v, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def _text(v, n: int) -> str:
    return "\n".join(line.rstrip() for line in str(v or "").replace("\r", "").split("\n"))[:n].strip()


def find_order(db: Session, number: str, email: str) -> ShopOrder | None:
    m = re.fullmatch(r"[Ww]?(\d{4,})", (number or "").strip())
    if not m or not email:
        return None
    so = db.get(ShopOrder, int(m.group(1)) - 1000)
    return so if so and so.email == email.strip().lower() else None


def open_ticket(db: Session, rt, data: dict) -> Ticket:
    """Raises ValueError (customer-safe text)."""
    if str(data.get("website", "")).strip():  # hidden honeypot field: only bots fill it
        raise ValueError("Request samajh nahi aayi.")
    kind = data.get("kind") if data.get("kind") in KINDS else "issue"
    name, email = _clean(data.get("name"), 120), _clean(data.get("email"), 254).lower()
    msg = _text(data.get("message"), 3000)
    if not _EMAIL.match(email):
        raise ValueError("Email sahi nahi lag raha.")
    if len(msg) < 5:
        raise ValueError("Apni baat thodi detail mein likho (kam se kam 5 akshar).")
    num = _clean(data.get("order"), 32)
    so = find_order(db, num, email) if num else None
    if num and so is None:
        raise ValueError("Ye order number aur email aapas mein match nahi karte. Order number (jaise W1001) aur wahi email daalo jo order par tha, ya order number khali chhodo.")
    try:
        rating = max(0, min(5, int(data.get("rating") or 0)))
    except (TypeError, ValueError):
        rating = 0
    t = Ticket(token=secrets.token_urlsafe(16), kind=kind, name=name, email=email, subject=_clean(data.get("subject"), 200) or KINDS[kind],
               order_number=f"W{1000 + so.id}" if so else "", shop_order_id=so.id if so else None, rating=rating if kind == "feedback" else 0)
    t.messages.append(TicketMessage(who="customer", body=msg))
    db.add(t)
    db.flush()
    try:
        rt.alert(db, level="warning" if kind in ("issue", "return") else "info", source="support",
                 message=f"Naya {KINDS[kind].lower()} #{t.id} ({name or email}" + (f", order {t.order_number}" if so else "") + "): " + msg[:120],
                 dedupe_key=f"ticket-{t.id}")
    except Exception:
        pass
    _mail(db, t, f"We got your message (#{t.id})", f"Hi {name or 'there'},\n\nWe received your {KINDS[kind].lower()} and will reply soon. You can read replies and add details here:\n{{link}}\n\n{C.get(db, 'shop_name')}")
    return t


def _mail(db: Session, t: Ticket, subject: str, body: str, base: str = "") -> None:
    """Best effort, from YOUR mailbox, only if connected and customer emails are on."""
    try:
        from . import mailer
        if not (mailer.configured(db) and mailer.customer_enabled(db)):
            return
        base = base or C.get(db, "shop_site_url").rstrip("/")
        link = f"{base}/#ticket={t.token}" if base else f"(open the shop and use Help > ticket #{t.id})"
        mailer.send_logged(db, t.email, f"{C.get(db, 'shop_name')}: {subject}", body.replace("{link}", link), "system")
    except Exception:
        pass


def thread(t: Ticket) -> dict:
    return {"id": t.id, "kind": t.kind, "status": t.status, "subject": t.subject, "order": t.order_number, "token": t.token,
            "messages": [{"who": m.who, "body": m.body, "when": m.created_at.isoformat()} for m in t.messages]}


def customer_reply(db: Session, t: Ticket, text: str) -> None:
    text = _text(text, 3000)
    if len(text) < 2:
        raise ValueError("Kuch likho.")
    if len(t.messages) >= 60:
        raise ValueError("Is ticket mein bahut messages ho gaye. Naya ticket banao.")
    t.messages.append(TicketMessage(who="customer", body=text))
    t.status, t.updated_at = "open", utcnow()
    db.flush()


def admin_reply(db: Session, t: Ticket, text: str, base: str = "") -> None:
    text = _text(text, 3000)
    if len(text) < 2:
        raise ValueError("Jawab khali hai.")
    t.messages.append(TicketMessage(who="admin", body=text))
    t.status, t.updated_at = "answered", utcnow()
    db.flush()
    _mail(db, t, f"Reply to your message (#{t.id})", f"Hi {t.name or 'there'},\n\n{text}\n\nSee the full conversation or reply here:\n{{link}}\n\n{C.get(db, 'shop_name')}", base)


def overdue(db: Session, hours: int = 24) -> int:
    return db.query(Ticket).filter(Ticket.status == "open", Ticket.updated_at < utcnow() - timedelta(hours=hours)).count()


# ---- reviews ---------------------------------------------------------------------------------------
def add_review(db: Session, rt, data: dict) -> Review:
    so = find_order(db, str(data.get("order", "")), str(data.get("email", "")))
    if so is None or so.status != "paid":
        raise ValueError("Review sirf paid order ke baad likh sakte ho. Order number aur wahi email daalo jo order par tha.")
    try:
        pid, rating = int(data.get("product_id")), int(data.get("rating"))
    except (TypeError, ValueError):
        raise ValueError("Product aur rating chuno.")
    import json
    if pid not in {int(i["id"]) for i in json.loads(so.items_json or "[]")}:
        raise ValueError("Ye product is order mein nahi tha.")
    if not 1 <= rating <= 5:
        raise ValueError("Rating 1 se 5 ke beech chuno.")
    if db.query(Review).filter(Review.shop_order_id == so.id, Review.product_id == pid).first():
        raise ValueError("Is order ke liye is product ka review aap pehle hi de chuke ho.")
    body = _text(data.get("body"), 1500)
    if len(body) < 5:
        raise ValueError("Review thoda detail mein likho.")
    rv = Review(product_id=pid, shop_order_id=so.id, rating=rating, name=_clean(data.get("name") or so.customer_name.split(" ")[0], 60), title=_clean(data.get("title"), 120), body=body)
    db.add(rv)
    db.flush()
    try:
        rt.alert(db, level="info", source="support", message=f"Naya review ({rating}/5) approve karne ke liye intezar kar raha hai.", dedupe_key=f"review-{rv.id}")
    except Exception:
        pass
    return rv


def ratings(db: Session) -> dict[int, tuple[float, int]]:
    rows = db.query(Review.product_id, func.avg(Review.rating), func.count(Review.id)).filter(Review.status == "approved").group_by(Review.product_id).all()
    return {pid: (round(float(avg), 1), int(n)) for pid, avg, n in rows}


def approved_reviews(db: Session, product_id: int, limit: int = 30) -> list[dict]:
    rows = db.query(Review).filter(Review.product_id == product_id, Review.status == "approved").order_by(Review.id.desc()).limit(limit).all()
    return [{"rating": r.rating, "name": r.name, "title": r.title, "body": r.body, "when": r.created_at.date().isoformat()} for r in rows]
