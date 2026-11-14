"""Earnings: what the affiliate networks say we earned, pulled through THEIR APIs (Awin, impact.com, CJ Affiliate).
Nothing here is estimated or invented: a row exists only because a network returned it. Clicks come from our own /shop/go counter.

Verification status (be honest): the Awin endpoint, auth, parameters and the 31-day window come from Awin's docs. The impact.com and CJ request/response
formats are written from memory of their public docs (their doc sites could not be read here) and parsing is tolerant; none of the three was run against
a real account. If a network answers with an error, the Affiliate/Earnings agent shows the exact message."""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import quote, urlencode

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..connectors import affiliate_sources as AS
from ..db import utcnow
from ..models import Click, Connection, Earning
from . import fx
from . import settings_service as S
from .agents import event

log = logging.getLogger("cheapedeal.earnings")
AGENT = "affiliate"
NETWORKS = {"aff_awin": "Awin", "aff_impact": "impact.com", "aff_cj_affiliate": "CJ Affiliate"}
SUB_PARAM = {"aff_awin": "clickref", "aff_impact": "subId1", "aff_cj_affiliate": "sid"}  # where the network lets us tag a click
WINDOW_DAYS = 31  # Awin's documented maximum range; we use it for CJ as well, and impact is paged


def dec(v) -> Decimal:
    try:
        return Decimal(str(v).replace(",", "").strip() or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def norm_status(raw: str) -> str:
    s = (raw or "").strip().lower()
    if any(w in s for w in ("revers", "declin", "delet", "reject", "cancel", "void")):
        return "declined"
    if "paid" in s or s == "closed":
        return "paid"
    if any(w in s for w in ("approv", "locked", "confirm", "valid")):
        return "approved"
    return "pending"


def parse_when(raw) -> datetime:
    t = str(raw or "").strip().replace("Z", "+00:00")
    for f in (lambda x: datetime.fromisoformat(x), lambda x: datetime.strptime(x[:19], "%Y-%m-%d %H:%M:%S")):
        try:
            d = f(t)
            return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
        except ValueError:
            continue
    return utcnow()


def _get_json(method: str, url: str, headers: dict, body: Optional[bytes], what: str):
    status, raw = AS.request(method, url, headers, body, timeout=60)
    if status in (401, 403):
        raise AS.SourceError(f"{what}: token/permission galat hai (HTTP {status}).")
    if status == 429:
        raise AS.SourceError(f"{what}: bahut zyada requests (HTTP 429), thodi der baad.", retryable=True)
    if status >= 400:
        raise AS.SourceError(f"{what}: HTTP {status} {raw[:160].decode('utf-8', 'replace')}")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        raise AS.SourceError(f"{what}: JSON nahi mila (galat ID/token ho sakta hai).")


def _money(node):
    """Awin sends {"amount": 1.2, "currency": "GBP"}; accept a bare number too."""
    if isinstance(node, dict):
        return dec(node.get("amount")), str(node.get("currency") or "")
    return dec(node), ""


# ---------------------------------------------------------------- adapters: (secrets, cfg, start, end) -> list of normalized rows
def fetch_awin(sec: dict, cfg: dict, start: datetime, end: datetime) -> list[dict]:
    pid, tok = (sec.get("awin_publisher_id") or cfg.get("awin_publisher_id") or "").strip(), (sec.get("awin_api_token") or "").strip()
    if not pid or not tok:
        return []
    q = urlencode({"startDate": start.strftime("%Y-%m-%dT%H:%M:%S"), "endDate": end.strftime("%Y-%m-%dT%H:%M:%S"), "timezone": "UTC"})
    data = _get_json("GET", f"https://api.awin.com/publishers/{quote(pid)}/transactions/?{q}", {"Authorization": f"Bearer {tok}", "Accept": "application/json"}, None, "Awin transactions")
    rows = []
    for t in data if isinstance(data, list) else []:
        amt, cur = _money(t.get("commissionAmount"))
        sale, scur = _money(t.get("saleAmount"))
        refs = t.get("clickRefs") or {}
        sub = str(t.get("clickRef") or (refs.get("clickRef") if isinstance(refs, dict) else "") or "")
        rows.append({"tx_id": str(t.get("id") or ""), "status": norm_status(str(t.get("commissionStatus") or "")), "amount": amt, "currency": cur or "USD",
                     "sale": sale, "sale_cur": scur or cur or "USD", "when": parse_when(t.get("transactionDate")),
                     "merchant": str(t.get("advertiserName") or t.get("advertiserId") or ""), "sub": sub})
    return rows


def fetch_impact(sec: dict, cfg: dict, start: datetime, end: datetime) -> list[dict]:
    sid, tok = (sec.get("impact_account_sid") or cfg.get("impact_account_sid") or "").strip(), (sec.get("impact_auth_token") or "").strip()
    if not sid or not tok:
        return []
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    q = urlencode({"ActionDateStart": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "ActionDateEnd": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "PageSize": 1000})
    url = f"https://api.impact.com/Mediapartners/{quote(sid)}/Actions?{q}"
    rows = []
    for _ in range(10):  # follow @nextpage, never loop forever
        data = _get_json("GET", url, {"Authorization": f"Basic {auth}", "Accept": "application/json"}, None, "impact.com Actions")
        for a in (data.get("Actions") if isinstance(data, dict) else None) or []:
            rows.append({"tx_id": str(a.get("Id") or ""), "status": norm_status(str(a.get("State") or a.get("Status") or "")), "amount": dec(a.get("Payout")),
                         "currency": str(a.get("Currency") or "USD"), "sale": dec(a.get("Amount")), "sale_cur": str(a.get("Currency") or "USD"),
                         "when": parse_when(a.get("EventDate") or a.get("CreationDate")), "merchant": str(a.get("CampaignName") or ""), "sub": str(a.get("SubId1") or "")})
        nxt = data.get("@nextpage") if isinstance(data, dict) else ""
        if not nxt:
            break
        url = "https://api.impact.com" + nxt if str(nxt).startswith("/") else str(nxt)
        if not url.startswith("https://api.impact.com/"):
            break
    return rows


CJ_QUERY = ('{ publisherCommissions(forPublishers: ["%s"], sincePostingDate: "%s", beforePostingDate: "%s") { count payloadComplete records '
            '{ commissionId actionStatus pubCommissionAmountUsd saleAmountUsd advertiserName postingDate %s } } }')


def fetch_cj(sec: dict, cfg: dict, start: datetime, end: datetime) -> list[dict]:
    pid, tok = (sec.get("cj_publisher_id") or cfg.get("cj_publisher_id") or "").strip(), (sec.get("cj_token") or "").strip()
    if not pid or not tok:
        return []
    hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Accept": "application/json"}
    data = None
    for extra in ("shopperId", ""):  # sub-id field name is the least certain part: retry without it
        body = json.dumps({"query": CJ_QUERY % (re.sub(r"[^0-9]", "", pid), start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ"), extra)}).encode()
        data = _get_json("POST", "https://commissions.api.cj.com/query", hdr, body, "CJ commissions")
        if not (isinstance(data, dict) and data.get("errors")):
            break
    if isinstance(data, dict) and data.get("errors"):
        raise AS.SourceError("CJ commissions: " + str(data["errors"][0].get("message", data["errors"][0]))[:200])
    recs = (((data or {}).get("data") or {}).get("publisherCommissions") or {}).get("records") or []
    return [{"tx_id": str(r.get("commissionId") or ""), "status": norm_status(str(r.get("actionStatus") or "")), "amount": dec(r.get("pubCommissionAmountUsd")),
             "currency": "USD", "sale": dec(r.get("saleAmountUsd")), "sale_cur": "USD", "when": parse_when(r.get("postingDate")),
             "merchant": str(r.get("advertiserName") or ""), "sub": str(r.get("shopperId") or "")} for r in recs]


FETCHERS = {"aff_awin": fetch_awin, "aff_impact": fetch_impact, "aff_cj_affiliate": fetch_cj}


class EarningsOnlySource:
    """A network connection with API credentials but no product feed: nothing to import, only earnings to read. Its test checks the credentials."""
    kind = "affiliate"
    supports_lookup = False

    def __init__(self, key: str, cfg: dict, sec: dict, program: dict):
        self.key, self.cfg, self.sec, self.program = key, cfg, sec, program
        self.name, self.merchant = key, program["label"]

    def search(self, query: str = "", limit: int = 50, page: int = 1) -> list:
        return []

    def test_connection(self) -> str:
        if not any(self.sec.get(k) for k in _cred_keys(self.key)):
            raise AS.SourceError("Product feed link ya earnings ke API credentials, kam se kam ek daalo.")
        end = utcnow()
        rows = FETCHERS[self.key](self.sec, self.cfg, end - timedelta(days=2), end)
        return f"{NETWORKS[self.key]} ki earnings API theek chali (pichhle 2 din mein {len(rows)} transaction)."


# ---------------------------------------------------------------- sync
def _usd(db: Session, amount: Decimal, cur: str) -> Optional[Decimal]:
    try:
        return fx.to_usd(db, amount, cur or "USD")
    except fx.FxError:
        return None


def upsert(db: Session, conn: Connection, rows: list[dict]) -> dict:
    new = changed = 0
    for r in rows:
        if not r["tx_id"]:
            continue
        e = db.query(Earning).filter(Earning.network == conn.type, Earning.tx_id == r["tx_id"][:80]).first()
        pid = int(r["sub"][1:]) if re.fullmatch(r"p\d{1,9}", r.get("sub") or "") else None
        usd, sale = _usd(db, r["amount"], r["currency"]), _usd(db, r["sale"], r.get("sale_cur") or r["currency"])
        if e is None:
            db.add(Earning(network=conn.type, connection_id=conn.id, tx_id=r["tx_id"][:80], status=r["status"], merchant=r["merchant"][:120], amount=r["amount"],
                           currency=(r["currency"] or "USD")[:8], amount_usd=usd, sale_usd=sale, occurred_at=r["when"], sub=(r.get("sub") or "")[:80], product_id=pid))
            new += 1
        elif (e.status, e.amount, e.amount_usd) != (r["status"], r["amount"], usd):
            e.status, e.amount, e.amount_usd, e.sale_usd = r["status"], r["amount"], usd, sale
            changed += 1
    db.flush()
    return {"new": new, "changed": changed}


def sync_connection(db: Session, rt, conn: Connection, *, days: int = 60, now: Optional[datetime] = None) -> dict:
    fetch = FETCHERS.get(conn.type)
    if fetch is None:
        return {"new": 0, "changed": 0, "skipped": True}
    sec, cfg = rt.connectors.secrets_of(conn), json.loads(conn.config_json or "{}")
    end = now or utcnow()
    start = end - timedelta(days=days)  # the whole window is re-read each time: networks change a status (pending -> approved/declined) weeks later
    out = {"new": 0, "changed": 0, "rows": 0}
    cur = start
    while cur < end:
        nxt = min(end, cur + timedelta(days=WINDOW_DAYS - 1))
        rows = fetch(sec, cfg, cur, nxt)
        out["rows"] += len(rows)
        got = upsert(db, conn, rows)
        out["new"] += got["new"]
        out["changed"] += got["changed"]
        cur = nxt
    S.set_value(db, f"earn_last_{conn.id}", end.isoformat())
    return out


def _cred_keys(t: str) -> list[str]:
    return {"aff_awin": ["awin_api_token"], "aff_impact": ["impact_auth_token"], "aff_cj_affiliate": ["cj_token"]}.get(t, [])


def run(db: Session, rt) -> dict:
    """The earnings job: for every connected network that has API credentials, pull the latest commissions."""
    if S.get_bool(db, "paused"):
        return {"paused": True}
    conns = [c for c in db.query(Connection).filter(Connection.kind == "affiliate", Connection.enabled.is_(True), Connection.type.in_(list(FETCHERS))).order_by(Connection.id).all()
             if any(rt.connectors.secrets_of(c).get(k) for k in _cred_keys(c.type))]
    if not conns:
        return {"none": True}
    out = {"new": 0, "changed": 0, "errors": 0, "networks": len(conns)}
    for c in conns:
        try:
            r = sync_connection(db, rt, c, days=90 if not S.get(db, f"earn_last_{c.id}", "") else 60)
            out["new"] += r["new"]
            out["changed"] += r["changed"]
            if r["new"]:
                event(db, AGENT, f"{NETWORKS.get(c.type, c.type)}: {r['new']} nayi commission aayi.", "ok")
        except Exception as e:  # one network failing must never stop the others
            db.rollback()
            out["errors"] += 1
            event(db, AGENT, f"{NETWORKS.get(c.type, c.type)} earnings: {str(e)[:200]}", "warn")
        db.commit()
    return out


# ---------------------------------------------------------------- numbers for the pages
def totals(db: Session, days: Optional[int] = None) -> dict:
    q = db.query(Earning.status, func.coalesce(func.sum(Earning.amount_usd), 0), func.count(Earning.id)).filter(Earning.status != "declined")
    if days:
        q = q.filter(Earning.occurred_at >= utcnow() - timedelta(days=days))
    by = {s: (Decimal(str(v)), n) for s, v, n in q.group_by(Earning.status).all()}
    z = (Decimal("0"), 0)
    pend, appr, paid = by.get("pending", z), by.get("approved", z), by.get("paid", z)
    return {"pending": pend[0], "approved": appr[0], "paid": paid[0], "total": pend[0] + appr[0] + paid[0], "count": pend[1] + appr[1] + paid[1],
            "no_rate": db.query(Earning).filter(Earning.amount_usd.is_(None)).count()}


def click_stats(db: Session) -> dict:
    now = utcnow()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    n = lambda since: db.query(Click).filter(Click.created_at >= since).count()  # noqa: E731
    return {"today": n(day), "week": n(now - timedelta(days=7)), "month": n(now - timedelta(days=30))}


def epc(db: Session) -> Optional[Decimal]:
    """Earnings per click over the last 30 days (None when there are no clicks)."""
    c = click_stats(db)["month"]
    return (totals(db, 30)["total"] / c).quantize(Decimal("0.0001")) if c else None


def with_subid(conn_type: str, url: str, pid: int) -> str:
    """Tag the click with p<product id> where the network supports it, so a commission can be tied back to the product that earned it."""
    key = SUB_PARAM.get(conn_type)
    if not key or not url:
        return url
    base, _, frag = url.partition("#")
    q = base.partition("?")[2]
    if any(part.split("=")[0].lower() == key.lower() for part in q.split("&") if part):
        return url
    return base + ("&" if "?" in base else "?") + f"{key}=p{pid}" + ("#" + frag if frag else "")


def prune_clicks(db: Session, days: int = 120) -> None:
    db.query(Click).filter(Click.created_at < utcnow() - timedelta(days=days)).delete(synchronize_session=False)
