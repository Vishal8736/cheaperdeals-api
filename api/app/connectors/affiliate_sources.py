"""Where affiliate offers come from. Three shapes:
  FeedSource            a product feed URL from ANY network/merchant (CSV/XML/JSON, gzip ok). Works for every program that gives you a feed.
  AmazonCreatorsSource  Amazon Associates' Creators API (the old PA-API 5 was shut down in 2026).
  EbaySource            eBay Browse API with your eBay Partner Network campaign id (the links come back already tracked).
Each has test_connection() and search(query, limit) -> list[Offer]. They only READ; nothing here buys or orders anything."""
from __future__ import annotations

import base64
import ipaddress
import socket
import time
from typing import Callable, Optional
from urllib.parse import quote, urlparse

import httpx

from ..adapters.base import SupplierError
from .feedparse import FeedParseError, MAX_BYTES, Offer, parse, parse_price

HTTP: Optional[Callable] = None  # tests replace this: HTTP(method, url, headers, body_bytes|None) -> (status, bytes)
CACHE_SECONDS = 600
_feed_cache: dict[str, tuple[float, list[Offer]]] = {}


class SourceError(SupplierError):
    pass


def public_url(url: str) -> str:
    """The feed address must be a normal public http(s) address (never localhost or a private network)."""
    u = urlparse((url or "").strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SourceError("Feed URL http:// ya https:// se shuru hona chahiye.")
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80)):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise SourceError("Ye address private/local network ka hai: sirf public feed URL chalta hai.")
    except socket.gaierror:
        pass  # cannot resolve: the real request will fail with its own message
    return url.strip()


def request(method: str, url: str, headers: Optional[dict] = None, body: Optional[bytes] = None, timeout: float = 60.0) -> tuple[int, bytes]:
    if HTTP is not None:
        return HTTP(method, url, headers or {}, body)
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0), follow_redirects=False) as c:
            with c.stream(method, url, headers=headers, content=body) as r:
                chunks, size = [], 0
                for ch in r.iter_bytes():
                    size += len(ch)
                    if size > MAX_BYTES:
                        raise SourceError("Jawab bahut bada hai (40 MB se zyada).")
                    chunks.append(ch)
                return r.status_code, b"".join(chunks)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # nothing was sent: safe to try again
        raise SourceError(f"Connection nahi bana: {type(e).__name__}", retryable=True)
    except httpx.HTTPError as e:  # sent but no answer: we cannot know what the server did
        raise SourceError(f"Server ne time par jawab nahi diya ({type(e).__name__}).", ambiguous=True)


def _json(status: int, raw: bytes, what: str):
    import json
    if status in (401, 403):
        raise SourceError(f"{what}: key/permission galat hai (HTTP {status}).")
    if status == 429:
        raise SourceError(f"{what}: bahut zyada requests (HTTP 429), thodi der baad try karo.", retryable=True)
    if status >= 400:
        raise SourceError(f"{what}: HTTP {status} {raw[:160].decode('utf-8', 'replace')}")
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise SourceError(f"{what}: jawab JSON nahi tha.")


def _match(o: Offer, words: list[str]) -> bool:
    hay = f"{o.title} {o.category} {o.brand} {o.description[:200]}".lower()
    return all(w in hay for w in words)


class FeedSource:
    kind = "affiliate"
    supports_lookup = False

    def __init__(self, cfg: dict, sec: dict, program: dict):
        self.cfg, self.sec, self.program = cfg, sec, program
        self.name = program["key"]
        self.merchant = (cfg.get("merchant") or program["label"]).strip()

    # ---- feed ----------------------------------------------------------------------------
    def _decorate(self, o: Offer) -> Offer:
        pre = (self.cfg.get("deeplink_prefix") or "").strip()
        if pre:  # wrap the plain link with the network's tracking address, e.g. https://track.example/?id=123&url={url_enc}
            if "{url" not in pre:
                pre += "{url_enc}"
            o.url = pre.replace("{url_enc}", quote(o.url, safe="")).replace("{url}", o.url)
        if o.commission_pct is None and self.cfg.get("commission_pct"):
            o.commission_pct = parse_price(self.cfg["commission_pct"])
        if not o.currency:
            o.currency = (self.cfg.get("currency") or "USD").strip().upper()[:3]
        return o

    def load(self) -> list[Offer]:
        url = public_url(self.sec.get("feed_url", ""))
        hit = _feed_cache.get(url)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return [o for o in hit[1]]
        status, raw = request("GET", url, {"Accept": "*/*", "User-Agent": "CheapeDeal-feed/18"})
        if status in (401, 403):
            raise SourceError("Feed ne mana kiya (HTTP %d): link purana ya key galat." % status)
        if status >= 400:
            raise SourceError(f"Feed nahi khula (HTTP {status}).")
        try:
            offers = parse(raw)
        except FeedParseError as e:
            raise SourceError(str(e))
        _feed_cache[url] = (time.monotonic(), offers)
        return list(offers)

    def test_connection(self) -> str:
        offers = self.load()
        o = offers[0]
        return f"Feed theek hai: {len(offers)} product mile. Pehla: {o.title[:50]} ({o.currency or '?'} {o.price})."

    def search(self, query: str = "", limit: int = 50, page: int = 1) -> list[Offer]:
        words = [w for w in (query or "").lower().split() if w]
        offers = [self._decorate(Offer(**{**o.__dict__})) for o in self.load() if _match(o, words)]
        start = max(0, (page - 1) * limit)
        return offers[start:start + limit]


class AmazonCreatorsSource:
    """Creators API (replaces PA-API 5). Endpoints/fields from Amazon's Creators API docs; the SearchItems path is the same pattern as the documented
    GetItems path (/catalog/v1/getItems) and was not run live."""
    kind = "affiliate"
    supports_lookup = False
    CATALOG = "https://creatorsapi.amazon/catalog/v1/"
    TOKEN = {"2.1": "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token", "2.2": "https://creatorsapi.auth.eu-south-2.amazoncognito.com/oauth2/token",
             "2.3": "https://creatorsapi.auth.us-west-2.amazoncognito.com/oauth2/token", "3.1": "https://api.amazon.com/auth/o2/token",
             "3.2": "https://api.amazon.co.uk/auth/o2/token", "3.3": "https://api.amazon.co.jp/auth/o2/token"}

    def __init__(self, cfg: dict, sec: dict, program: dict):
        self.cfg, self.sec, self.program = cfg, sec, program
        self.name = program["key"]
        self.merchant = program["label"].split(" (")[0]
        self.version = (cfg.get("credential_version") or "3.1").strip()
        self._tok: tuple[str, float] = ("", 0.0)

    def _token(self) -> str:
        if self._tok[0] and time.monotonic() < self._tok[1]:
            return self._tok[0]
        if self.version not in self.TOKEN:
            raise SourceError("Credential version 2.1/2.2/2.3 ya 3.1/3.2/3.3 hona chahiye (Associates portal mein credential ke saath likha hota hai).")
        import json
        cid, csec = self.sec.get("client_id", ""), self.sec.get("client_secret", "")
        if self.version.startswith("3"):
            st, raw = request("POST", self.TOKEN[self.version], {"Content-Type": "application/json"},
                              json.dumps({"grant_type": "client_credentials", "client_id": cid, "client_secret": csec, "scope": "creatorsapi::default"}).encode())
        else:
            basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
            st, raw = request("POST", self.TOKEN[self.version], {"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"},
                              b"grant_type=client_credentials&scope=creatorsapi/default")
        d = _json(st, raw, "Amazon login")
        tok = d.get("access_token")
        if not tok:
            raise SourceError("Amazon ne access token nahi diya: Client ID/Secret aur credential version check karo.")
        self._tok = (tok, time.monotonic() + max(60, int(d.get("expires_in", 3600)) - 120))
        return tok

    def search(self, query: str = "", limit: int = 10, page: int = 1) -> list[Offer]:
        import json
        mk = (self.cfg.get("marketplace") or "www.amazon.com").strip()
        auth = f"Bearer {self._token()}" + (f", Version {self.version}" if self.version.startswith("2") else "")
        body = {"keywords": query or "gift", "searchIndex": "All", "itemCount": max(1, min(10, limit)), "itemPage": max(1, min(10, page)),
                "resources": ["images.primary.large", "itemInfo.title", "offersV2.listings.price"], "partnerTag": self.cfg.get("partner_tag", ""), "marketplace": mk}
        st, raw = request("POST", self.CATALOG + "searchItems", {"Authorization": auth, "Content-Type": "application/json", "x-marketplace": mk}, json.dumps(body).encode())
        d = _json(st, raw, "Amazon search")
        out = []
        for it in (d.get("searchResult") or {}).get("items") or []:
            try:
                title = it["itemInfo"]["title"]["displayValue"]
                url = it["detailPageURL"]
                lst = (it.get("offersV2") or {}).get("listings") or []
                money = lst[0]["price"]["money"] if lst else {}
                price = parse_price(money.get("amount"))
            except (KeyError, TypeError, IndexError):
                continue
            if price is None:  # an item without a price is not shown: we never invent one
                continue
            img = (((it.get("images") or {}).get("primary") or {}).get("large") or {}).get("url", "")
            out.append(Offer(offer_id=str(it.get("asin") or url[-20:]), title=title[:250], price=price, currency=str(money.get("currency") or "USD"), image=img, url=url,
                             category="Amazon", commission_pct=None))
        return out

    def test_connection(self) -> str:
        if not self.cfg.get("partner_tag"):
            raise SourceError("Partner tag (Associate tag) chahiye.")
        got = self.search("test", 1)
        return f"Amazon Creators API se login ho gaya, search chali ({len(got)} item)."


class EbaySource:
    kind = "affiliate"
    supports_lookup = False

    def __init__(self, cfg: dict, sec: dict, program: dict):
        self.cfg, self.sec, self.program = cfg, sec, program
        self.name = program["key"]
        self.merchant = "eBay"
        self._tok: tuple[str, float] = ("", 0.0)

    def _token(self) -> str:
        if self._tok[0] and time.monotonic() < self._tok[1]:
            return self._tok[0]
        basic = base64.b64encode(f"{self.sec.get('client_id', '')}:{self.sec.get('client_secret', '')}".encode()).decode()
        st, raw = request("POST", "https://api.ebay.com/identity/v1/oauth2/token", {"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"},
                          b"grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope")
        d = _json(st, raw, "eBay login")
        if not d.get("access_token"):
            raise SourceError("eBay ne token nahi diya: App ID / Cert ID check karo.")
        self._tok = (d["access_token"], time.monotonic() + max(60, int(d.get("expires_in", 7200)) - 120))
        return d["access_token"]

    def search(self, query: str = "", limit: int = 20, page: int = 1) -> list[Offer]:
        mk = (self.cfg.get("marketplace") or "EBAY_US").strip().upper()
        ctx = f"affiliateCampaignId={self.cfg.get('campaign_id', '').strip()}" + (f",affiliateReferenceId={self.cfg['reference_id'].strip()}" if self.cfg.get("reference_id") else "")
        url = (f"https://api.ebay.com/buy/browse/v1/item_summary/search?q={quote(query or 'gift')}&limit={max(1, min(50, limit))}"
               f"&offset={max(0, (page - 1) * limit)}&filter=buyingOptions:%7BFIXED_PRICE%7D")
        st, raw = request("GET", url, {"Authorization": f"Bearer {self._token()}", "X-EBAY-C-MARKETPLACE-ID": mk, "X-EBAY-C-ENDUSERCTX": ctx})
        d = _json(st, raw, "eBay search")
        out = []
        for it in d.get("itemSummaries") or []:
            link = it.get("itemAffiliateWebUrl")  # only present when the campaign id was accepted: a plain eBay link would earn nothing
            price = parse_price((it.get("price") or {}).get("value"))
            if not link or price is None or not it.get("title"):
                continue
            out.append(Offer(offer_id=str(it.get("itemId", link[-30:])), title=str(it["title"])[:250], price=price, currency=str((it.get("price") or {}).get("currency") or "USD"),
                             image=str((it.get("image") or {}).get("imageUrl", "")), url=link, category=str(((it.get("categories") or [{}])[0]).get("categoryName", ""))))
        return out

    def test_connection(self) -> str:
        if not self.cfg.get("campaign_id"):
            raise SourceError("EPN Campaign ID chahiye (bina uske links par commission nahi milta).")
        got = self.search("watch", 3)
        if not got:
            raise SourceError("Search chali par tracked (affiliate) link nahi aaya: Campaign ID galat ya approve nahi hua.")
        return f"eBay Browse API chal raha hai, {len(got)} tracked item mile."
