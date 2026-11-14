"""Automatic market prices from eBay (official Browse API, free developer keys).

For every product (or offer) that has 'search words' we ask eBay what comparable items are listed for in each market and store the
MEDIAN as that market's price, source "api". Honest limits: these are ASKING prices of active listings, not sold prices; a bad
search phrase gives a bad price. That is why prices from here that look far too good (sanity ratio) still wait for you in Strategy.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import statistics
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from sqlalchemy.orm import Session

from .. import secretbox
from ..models import Market, Product
from . import settings_service as S
from .agents import event as agent_event

log = logging.getLogger("cheapedeal.pricefeed")
D = Decimal
HTTP = None  # tests replace this: HTTP(method, url, headers: dict, body: bytes|None) -> dict
SECRET_KEY = ""
MIN_LISTINGS = 5
MARKETPLACE = {"US": "EBAY_US", "GB": "EBAY_GB", "DE": "EBAY_DE", "AU": "EBAY_AU", "CA": "EBAY_CA", "FR": "EBAY_FR", "IT": "EBAY_IT", "ES": "EBAY_ES"}
_token_cache: dict = {}


class FeedError(Exception):
    pass


# ---- credentials ---------------------------------------------------------------------------
def credentials(db: Session) -> tuple[str, str]:
    cid, sec = os.getenv("EBAY_CLIENT_ID", ""), os.getenv("EBAY_CLIENT_SECRET", "")
    if cid and sec:
        return cid, sec
    enc = S.get(db, "ebay_creds_enc", "")
    if enc and SECRET_KEY:
        try:
            d = secretbox.unseal(SECRET_KEY, enc)
            return d.get("client_id", ""), d.get("client_secret", "")
        except Exception:
            return "", ""
    return "", ""


def save_credentials(db: Session, client_id: str, client_secret: str) -> None:
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id or not client_secret:
        raise ValueError("Both the App ID (client id) and the Cert ID (client secret) are needed")
    S.set_value(db, "ebay_creds_enc", secretbox.seal(SECRET_KEY, {"client_id": client_id, "client_secret": client_secret}))
    _token_cache.clear()


def enabled(db: Session) -> bool:
    return S.get_bool(db, "pricefeed_enabled") and all(credentials(db))


# ---- http ----------------------------------------------------------------------------------
def _http(method: str, url: str, headers: dict, body: bytes | None = None) -> dict:
    if HTTP is not None:
        return HTTP(method, url, headers, body)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raise FeedError(f"eBay answered {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    except (OSError, ValueError) as e:
        raise FeedError(f"Cannot reach eBay: {e}")


def _token(db: Session) -> str:
    import time
    cid, sec = credentials(db)
    if not (cid and sec):
        raise FeedError("eBay keys are not set")
    t = _token_cache.get(cid)
    if t and t[1] > time.time() + 60:
        return t[0]
    auth = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"}).encode()
    res = _http("POST", "https://api.ebay.com/identity/v1/oauth2/token",
                {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}, data)
    tok = res.get("access_token")
    if not tok:
        raise FeedError("eBay did not give a token: check the App ID and Cert ID")
    _token_cache[cid] = (tok, time.time() + int(res.get("expires_in", 7200)))
    return tok


# ---- the price -----------------------------------------------------------------------------
def robust_median(values: list[Decimal]) -> Decimal | None:
    """Median after throwing away silly listings (accessories at 10% of the price, scams at 5x). Needs MIN_LISTINGS to be trusted."""
    vals = sorted(v for v in values if v > 0)
    if len(vals) < MIN_LISTINGS:
        return None
    m = D(str(statistics.median(vals)))
    kept = [v for v in vals if m * D("0.4") <= v <= m * D("2.5")]
    if len(kept) < MIN_LISTINGS:
        return None
    return D(str(statistics.median(kept))).quantize(D("0.01"))


def lookup(db: Session, query: str, market: Market) -> tuple[Decimal | None, str]:
    """(median asking price in the market's currency or None, note)."""
    mp = MARKETPLACE.get(market.code.upper())
    if not mp:
        return None, f"no eBay site for {market.code}"
    q = urllib.parse.urlencode({"q": query, "limit": "50", "filter": "buyingOptions:{FIXED_PRICE},conditionIds:{1000}"})
    res = _http("GET", f"https://api.ebay.com/buy/browse/v1/item_summary/search?{q}",
                {"Authorization": f"Bearer {_token(db)}", "X-EBAY-C-MARKETPLACE-ID": mp})
    vals = []
    for it in res.get("itemSummaries", []) or []:
        pr = it.get("price") or {}
        if (pr.get("currency") or "").upper() == market.currency.upper():
            try:
                vals.append(D(str(pr.get("value"))))
            except Exception:
                pass
    m = robust_median(vals)
    if m is None:
        return None, f"only {len(vals)} usable listing(s) on {mp} (need {MIN_LISTINGS})"
    return m, f"median of {len(vals)} listings on {mp}"


def run(db: Session, rt) -> dict:
    from . import strategy as ST
    stats = {"looked_up": 0, "updated": 0, "skipped": 0, "errors": 0, "paused": False, "off": False}
    if S.get_bool(db, "paused"):
        stats["paused"] = True
        return stats
    if not enabled(db):
        stats["off"] = True
        return stats
    ST.seed_markets(db)
    markets = [m for m in db.query(Market).filter(Market.enabled.is_(True)).all() if m.code.upper() in MARKETPLACE]
    jobs = [("product", p) for p in db.query(Product).filter(Product.search_query != "", Product.active.is_(True), Product.kind != "affiliate").all()]
    cap = 60  # eBay's free allowance is generous but be polite: at most this many searches per run
    n = 0
    for kind, obj in jobs:
        for m in markets:
            if n >= cap:
                break
            n += 1
            stats["looked_up"] += 1
            try:
                price, note = lookup(db, obj.search_query, m)
            except FeedError as e:
                stats["errors"] += 1
                rt.alert(db, level="warning", source="pricefeed", dedupe_key="pf-err", message=f"eBay price feed problem: {e}")
                db.commit()
                return stats  # a broken key/network will fail every call: stop, do not hammer
            if price is None:
                stats["skipped"] += 1
                continue
            url = "https://www.ebay.com/sch/i.html?_nkw=" + urllib.parse.quote(obj.search_query)
            try:
                ST.record_price(db, obj, m, price, url=url, source="api")
                stats["updated"] += 1
            except ValueError as e:
                stats["skipped"] += 1
                log.info("price feed skipped %s: %s", obj, e)
        db.commit()
    S.mark_run(db, "price_watch", f"{stats['updated']} prices updated")
    if stats["updated"]:
        agent_event(db, "scout", f"Price watch: updated {stats['updated']} market price(s) from eBay (asking prices, median)", "ok")
    db.commit()
    return stats
