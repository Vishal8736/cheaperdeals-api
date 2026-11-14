"""Read a product feed in whatever shape an affiliate network hands it out: CSV / TSV / ; | separated, XML (RSS, Google Shopping, Admitad YML,
plain <product> lists), JSON, and any of them gzipped. Returns plain Offer rows. No network here, only parsing (fetching is in affiliate_sources)."""
from __future__ import annotations

import csv
import io
import json
import re
import zlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional
from xml.etree import ElementTree as ET

MAX_ROWS = 20000
MAX_BYTES = 40_000_000


class FeedParseError(Exception):
    pass


@dataclass
class Offer:
    offer_id: str
    title: str
    price: Decimal
    currency: str = ""
    image: str = ""
    url: str = ""
    description: str = ""
    category: str = ""
    brand: str = ""
    commission_pct: Optional[Decimal] = None


# the first alias that has a value wins, so put the most specific names first
ALIAS = {
    "id": ["aw_product_id", "product_id", "productid", "offer_id", "offerid", "item_id", "itemid", "id", "merchant_product_id", "sku", "mpn", "upc", "ean", "product_sku"],
    "title": ["product_name", "productname", "product_title", "title", "name", "item_name"],
    "price": ["search_price", "current_price", "currentprice", "sale_price", "saleprice", "display_price", "store_price", "price", "retail_price", "retailprice", "base_price"],
    "currency": ["currency", "currency_code", "currencyid", "price_currency"],
    "image": ["aw_image_url", "merchant_image_url", "large_image", "image_url", "imageurl", "image_link", "picture", "image", "thumb_url", "thumbnail", "aw_thumb_url"],
    "url": ["aw_deep_link", "affiliate_url", "affiliate_link", "tracking_url", "tracking_link", "buy_url", "buyurl", "deep_link", "deeplink", "link_url", "linkurl", "product_url", "producturl", "link", "url", "merchant_deep_link"],
    "description": ["product_short_description", "short_description", "description", "long_description", "desc", "product_description"],
    "category": ["category_name", "merchant_category", "advertiser_category", "category", "categorypath", "category_path", "product_type", "categoryid"],
    "brand": ["brand_name", "brand", "manufacturer"],
    "commission": ["commission_pct", "commission_rate", "commission_percent", "commission"],
}
LIST_KEYS = ("products", "items", "offers", "data", "results", "result", "entries", "records")
ITEM_TAGS = {"item", "entry", "product", "offer", "row", "record", "listing"}


def norm_key(k) -> str:
    k = str(k or "").strip().lower()
    k = k.split(":")[-1] if k.startswith("g:") else k
    return re.sub(r"[^a-z0-9]+", "_", k).strip("_")


def parse_price(v) -> Optional[Decimal]:
    """'1,299.00 USD', '₹1,299', '1.299,00 €', '19.99' -> Decimal. None if there is no positive number."""
    s = re.sub(r"[^\d.,]", "", str(v or "")).strip(".,")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d > 0 else None


def _currency_from(raw_price, explicit: str) -> str:
    if explicit and re.fullmatch(r"[A-Za-z]{3}", explicit.strip()):
        return explicit.strip().upper()
    m = re.search(r"\b([A-Z]{3})\b", str(raw_price or ""))
    return m.group(1) if m else ""


def _pick(row: dict, names: list[str]) -> str:
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _http_url(v: str) -> str:
    v = (v or "").strip()
    return v if re.match(r"^https?://", v, re.I) and len(v) <= 1500 else ""


def row_to_offer(row: dict) -> Optional[Offer]:
    r = {norm_key(k): v for k, v in row.items() if k is not None}
    title = _pick(r, ALIAS["title"])
    url = ""
    for n in ALIAS["url"]:  # first alias that is really a web link
        url = _http_url(str(r.get(n) or ""))
        if url:
            break
    raw_price = ""
    price = None
    for n in ALIAS["price"]:
        raw_price = str(r.get(n) or "")
        price = parse_price(raw_price)
        if price is not None:
            break
    if not title or not url or price is None:
        return None
    oid = _pick(r, ALIAS["id"]) or url[-80:]
    com = parse_price(_pick(r, ALIAS["commission"]).replace("%", ""))
    return Offer(offer_id=oid[:120], title=re.sub(r"\s+", " ", title)[:250], price=price.quantize(Decimal("0.01")),
                 currency=_currency_from(raw_price, _pick(r, ALIAS["currency"])), image=_http_url(_pick(r, ALIAS["image"])), url=url,
                 description=re.sub(r"<[^>]+>", " ", _pick(r, ALIAS["description"]))[:1500].strip(), category=_pick(r, ALIAS["category"])[:120],
                 brand=_pick(r, ALIAS["brand"])[:80], commission_pct=com if com is not None and com <= 100 else None)


def decompress(raw: bytes) -> bytes:
    if raw[:2] == b"\x1f\x8b":
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = d.decompress(raw, MAX_BYTES)
        if d.unconsumed_tail:
            raise FeedParseError("Feed bahut bada hai (40 MB se zyada khulne par). Chhota feed ya category-wise feed lo.")
        return out
    return raw


def _text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeError:
            continue
    return raw.decode("utf-8", "replace")


def _rows_csv(text: str) -> list[dict]:
    head = text[:4000].splitlines()[0] if text.strip() else ""
    delim = max([",", ";", "\t", "|"], key=lambda d: head.count(d))
    if head.count(delim) == 0:
        raise FeedParseError("Feed mein columns nahi mile (CSV/TSV nahi lagta).")
    csv.field_size_limit(5_000_000)
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))[:MAX_ROWS]


def _rows_xml(text: str) -> list[dict]:
    if re.search(r"<!(DOCTYPE|ENTITY)", text[:20000], re.I):
        raise FeedParseError("XML mein DOCTYPE/ENTITY hai: suraksha ke liye nahi padha jata.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise FeedParseError(f"XML sahi nahi hai: {e}")
    rows = []
    for el in root.iter():
        tag = el.tag.split("}")[-1].lower() if isinstance(el.tag, str) else ""
        if tag not in ITEM_TAGS or len(list(el)) < 2:
            continue
        row = {k.split("}")[-1]: v for k, v in el.attrib.items()}  # Admitad YML keeps the id in an attribute
        for ch in el:
            row[ch.tag.split("}")[-1]] = (ch.text or "").strip() if len(ch) == 0 else row.get(ch.tag.split("}")[-1], "")
        rows.append(row)
        if len(rows) >= MAX_ROWS:
            break
    return rows


def _rows_json(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise FeedParseError(f"JSON sahi nahi hai: {e}")
    for _ in range(3):
        if isinstance(data, dict):
            nxt = next((data[k] for k in LIST_KEYS if isinstance(data.get(k), (list, dict))), None)
            if nxt is None:
                break
            data = nxt
    if not isinstance(data, list):
        raise FeedParseError("JSON mein products ki list nahi mili.")
    out = []
    for d in data[:MAX_ROWS]:
        if isinstance(d, dict):
            flat = {}
            for k, v in d.items():
                if isinstance(v, (str, int, float)):
                    flat[k] = v
                elif isinstance(v, dict):
                    for kk, vv in v.items():
                        if isinstance(vv, (str, int, float)):
                            flat.setdefault(kk, vv)
            out.append(flat)
    return out


def parse(raw: bytes) -> list[Offer]:
    """Bytes of a feed -> usable offers (a row without a title, a web link and a positive price is skipped)."""
    raw = decompress(raw)
    if len(raw) > MAX_BYTES:
        raise FeedParseError("Feed bahut bada hai.")
    text = _text(raw).lstrip()
    if not text:
        raise FeedParseError("Feed khali hai.")
    rows = _rows_xml(text) if text[:1] == "<" else _rows_json(text) if text[:1] in "[{" else _rows_csv(text)
    offers, seen = [], set()
    for r in rows:
        o = row_to_offer(r)
        if o and o.offer_id not in seen:
            seen.add(o.offer_id)
            offers.append(o)
    if not offers:
        raise FeedParseError(f"{len(rows)} row padhe par ek bhi product nahi bana: title, link (http) aur price teeno chahiye. Feed ke column naam check karo.")
    return offers
