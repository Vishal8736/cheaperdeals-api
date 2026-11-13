"""Market research sources: read-only price/stock lookups on marketplaces that give a FREE API key. They cannot place orders.

Flipkart Affiliate API (affiliate.flipkart.com): headers Fk-Affiliate-Id + Fk-Affiliate-Token, keyword search
  GET https://affiliate-api.flipkart.net/affiliate/search/json?query=..&resultCount=..   (max 10 results; docs say it will be deprecated in favour of the feeds)
Shapes follow the official docs; not run live from the build sandbox."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from . import settings_service as S
from . import shopcfg as C

FLIPKART = "https://affiliate-api.flipkart.net/affiliate/search/json"


class ResearchError(Exception):
    pass


def flipkart_ready(db) -> bool:
    return bool(S.get(db, "flipkart_affid", "") and C.read_secret(db, "flipkart_token_enc"))


def _amt(d) -> Decimal | None:
    try:
        return Decimal(str((d or {}).get("amount")))
    except (InvalidOperation, TypeError):
        return None


def flipkart_search(db, query: str, n: int = 10) -> list[dict]:
    if not flipkart_ready(db):
        raise ResearchError("Flipkart Affiliate ID aur token pehle save karo.")
    if not query.strip():
        raise ResearchError("Product ka naam likho.")
    r = C.client().get(FLIPKART, params={"query": query.strip(), "resultCount": max(1, min(10, n))}, timeout=20,
                       headers={"Fk-Affiliate-Id": S.get(db, "flipkart_affid", ""), "Fk-Affiliate-Token": C.read_secret(db, "flipkart_token_enc")})
    if r.status_code in (401, 403):
        raise ResearchError("Flipkart ne token/ID mana kar diya. affiliate.flipkart.com/api/api-token se naya token banao.")
    if r.status_code == 429:
        raise ResearchError("Flipkart ne rok diya (limit 20 calls/second). Thodi der baad try karo.")
    if r.status_code >= 300:
        raise ResearchError(f"Flipkart ne HTTP {r.status_code} diya.")
    try:
        items = r.json().get("productInfoList") or []
    except ValueError:
        raise ResearchError("Flipkart ka jawab padh nahi paya.")
    out = []
    for it in items:
        b = it.get("productBaseInfo") or it.get("productBaseInfoV1") or {}
        a = b.get("productAttributes") or {}
        price = _amt(a.get("sellingPrice")) or _amt(a.get("flipkartSellingPrice"))
        if price is None:
            continue
        out.append({"title": str(a.get("title", ""))[:140], "price": price, "mrp": _amt(a.get("maximumRetailPrice")), "currency": (a.get("sellingPrice") or {}).get("currency", "INR"),
                    "in_stock": bool(a.get("inStock", True)), "url": a.get("productUrl", "")})
    return out


def compare(db, rt, query: str) -> dict:
    """Same keyword on Flipkart (INR) and on your supplier (USD converted to INR): a reference for where a product sells for more."""
    from . import catalog as CAT
    from . import fx
    flip = flipkart_search(db, query, 10)
    sup, err = [], ""
    try:
        for it in CAT.search(db, rt, 0, query, 1, 10):
            try:
                inr = fx.from_usd(db, it.cost, "INR")
            except Exception:
                inr = None
            sup.append({"title": it.title[:140], "cost_usd": it.cost, "cost_inr": inr})
    except Exception as e:
        err = f"Supplier search fail: {str(e)[:150]}"
    return {"flipkart": flip, "supplier": sup, "error": err}
