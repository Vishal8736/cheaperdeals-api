"""CJ Dropshipping supplier (API 2.0): you paste ONE API key, no login/authorize step.

Flow (CJ developer docs, developers.cjdropshipping.com):
  POST /api2.0/v1/authentication/getAccessToken {apiKey}      -> accessToken + refreshToken (valid about 180 days)
  every call carries header CJ-Access-Token; we renew with refreshAccessToken, and if that fails we simply ask again with the API key.
So nobody ever has to come back and authorize.
Not run against a live CJ account yet: field names follow the public docs (see docs/ASSUMPTIONS.md).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

import httpx

from ..adapters.base import (CatalogItem, SupplierError, SupplierOrderInfo, SupplierOrderRequest, SupplierOrderResult, SupplierProductInfo)
from .generic_rest import _dec, split_vid
from .rest_utils import dig

BASE = "https://developers.cjdropshipping.com/api2.0/v1"
STATUS = {"CREATED": "pending", "IN_CART": "pending", "UNPAID": "pending", "PENDING": "confirmed", "PROCESSING": "confirmed", "UNSHIPPED": "confirmed",
          "SHIPPED": "shipped", "DELIVERED": "delivered", "CANCELLED": "cancelled", "CANCELED": "cancelled"}
_HINTS = (
    ("apikey", "API key galat ya expire hai: CJ account se nayi key banao aur Edit mein daalo."),
    ("balance", "CJ wallet mein paise kam hain: CJ account mein balance add karo (order balance se pay hota hai)."),
    ("insufficient", "CJ wallet mein paise kam hain: CJ account mein balance add karo."),
    ("too many requests", "CJ ne rate limit lagayi (1 call/second). Thodi der baad apne aap dobara try hoga."),
    ("qps", "CJ ne rate limit lagayi (1 call/second). Thodi der baad apne aap dobara try hoga."),
    ("logistic", "Ye shipping method is product/desh ke liye nahi hai: Settings mein shipping method AUTO karo."),
    ("out of stock", "Stock khatam hai."),
    ("address", "Customer ka address CJ ko sahi nahi laga (city/province/zip/phone check karo)."),
)


def explain(msg: str) -> str:
    low = (msg or "").lower()
    for key, hint in _HINTS:
        if key in low:
            return f"{msg} -> {hint}"
    return msg


def _ts(v) -> float:
    """CJ dates look like '2025-01-01T10:00:00+08:00' or '2025-01-01 10:00:00'; unknown -> 0."""
    s = str(v or "").strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S"):
        try:
            d = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            continue
    return 0.0


class CJSupplier:
    name = "CJ Dropshipping"
    supports_lookup = False

    def __init__(self, creds: dict, token_load: Optional[Callable] = None, token_save: Optional[Callable] = None, client: Optional[httpx.Client] = None):
        self.creds = {k: v for k, v in (creds or {}).items() if v not in (None, "")}
        self.token_load, self.token_save = token_load, token_save
        self._mem: dict = {}
        self._client = client or httpx.Client(timeout=httpx.Timeout(25.0, connect=10.0))
        self.sleep = time.sleep

    # ---- tokens ------------------------------------------------------------------------------
    def tokens(self) -> dict:
        return (self.token_load() if self.token_load else self._mem) or {}

    def _store(self, t: dict) -> dict:
        if self.token_save:
            self.token_save(t)
        self._mem = t
        return t

    def _raw(self, method: str, path: str, *, params=None, body=None, token: str = "") -> dict:
        headers = {"Accept": "application/json", **({"CJ-Access-Token": token} if token else {})}
        try:
            r = self._client.request(method, BASE + path, params=params, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise SupplierError(f"CJ timed out: {e}", retryable=True)
        except httpx.HTTPError as e:
            raise SupplierError(f"Cannot reach CJ: {e}", retryable=True)
        if r.status_code == 429 or r.status_code >= 500:
            raise SupplierError(f"CJ HTTP {r.status_code}", retryable=True, status_code=r.status_code)
        try:
            data = r.json()
        except ValueError:
            raise SupplierError(f"CJ did not return JSON (HTTP {r.status_code})")
        if not isinstance(data, dict):
            raise SupplierError("CJ answered in an unexpected shape")
        return data

    @staticmethod
    def _ok(data: dict) -> bool:
        return data.get("result") is True or (data.get("result") is None and int(data.get("code") or 0) == 200)

    def _fresh_token(self) -> dict:
        key = (self.creds.get("api_key") or "").strip()
        if not key:
            raise SupplierError("CJ API key khali hai: Connect > CJ > Edit mein daalo.", retryable=False)
        d = self._raw("POST", "/authentication/getAccessToken", body={"apiKey": key})
        if not self._ok(d) or not (d.get("data") or {}).get("accessToken"):
            raise SupplierError(explain(f"CJ ne API key nahi mani: {d.get('message') or d.get('msg') or str(d)[:150]}"), retryable=False)
        x = d["data"]
        now = time.time()
        return self._store({"access_token": x["accessToken"], "refresh_token": x.get("refreshToken", ""),
                            "access_expires_at": _ts(x.get("accessTokenExpiryDate")) or now + 14 * 86400,
                            "refresh_expires_at": _ts(x.get("refreshTokenExpiryDate")) or now + 150 * 86400})

    def ensure_token(self) -> dict:
        t = self.tokens()
        now = time.time()
        if t.get("access_token") and t.get("access_expires_at", 0) - now > 86400:
            return t
        if t.get("refresh_token") and t.get("refresh_expires_at", 0) - now > 86400:
            try:
                d = self._raw("POST", "/authentication/refreshAccessToken", body={"refreshToken": t["refresh_token"]})
                x = d.get("data") or {}
                if self._ok(d) and x.get("accessToken"):
                    return self._store({**t, "access_token": x["accessToken"], "refresh_token": x.get("refreshToken") or t["refresh_token"],
                                        "access_expires_at": _ts(x.get("accessTokenExpiryDate")) or now + 14 * 86400,
                                        "refresh_expires_at": _ts(x.get("refreshTokenExpiryDate")) or t.get("refresh_expires_at", now + 100 * 86400)})
            except SupplierError:
                pass  # fall through: the API key always works
        return self._fresh_token()

    # ---- calls -------------------------------------------------------------------------------
    def _call(self, method: str, path: str, *, params=None, body=None, ambiguous_on_timeout: bool = False) -> dict:
        tok = self.ensure_token()["access_token"]
        for attempt in (0, 1):
            try:
                d = self._raw(method, path, params=params, body=body, token=tok)
            except SupplierError as e:
                if e.retryable and ambiguous_on_timeout and "timed out" in str(e):
                    raise SupplierError(str(e), ambiguous=True, retryable=False)
                raise
            code = int(d.get("code") or 0)
            msg = str(d.get("message") or d.get("msg") or "")
            if code == 429 or "too many" in msg.lower() or "qps" in msg.lower():  # rate limit (1 call/second): wait and retry once
                if attempt == 0 and not ambiguous_on_timeout:
                    self.sleep(1.2)
                    continue
                raise SupplierError(explain(f"CJ: {msg or code}"), retryable=True)
            if "token" in msg.lower() and not self._ok(d):  # token no longer valid: get a new one once
                if attempt == 0:
                    tok = self._fresh_token()["access_token"]
                    continue
            if not self._ok(d):
                raise SupplierError(explain(f"CJ: {msg or d}"[:300]), retryable=False)
            return d
        raise SupplierError("CJ did not answer", retryable=True)

    # ---- catalog -----------------------------------------------------------------------------
    def list_catalog(self, query: str = "", page: int = 1, page_size: int = 20) -> list[CatalogItem]:
        d = self._call("GET", "/product/listV2", params={"keyWord": query or "", "page": page, "size": max(1, min(100, page_size)),
                                                          **({"countryCode": self.creds["ship_to_country"]} if self.creds.get("ship_to_country") else {})})
        out = []
        for block in (dig(d, "data.content", []) or []):
            for p in (block.get("productList") or []):
                title = p.get("nameEn") or p.get("productNameEn") or ""
                if not title:
                    continue
                out.append(CatalogItem(supplier_product_id=str(p.get("id") or p.get("pid") or ""), vid="", title=str(title), cost=_dec(p.get("nowPrice") or p.get("sellPrice") or "0"),
                                       sku=str(p.get("sku") or ""), image_url=str(p.get("bigImage") or ""), category=str(p.get("threeCategoryName") or p.get("categoryName") or "")))
        return out

    def _product(self, pid: str) -> dict:
        d = self._call("GET", "/product/query", params={"pid": pid})
        return d.get("data") or {}

    def variants_of(self, pid: str) -> list[dict]:
        return [{"vid": f"{pid}|{v.get('vid')}", "variantSellPrice": v.get("variantSellPrice"), "name": v.get("variantNameEn", "")}
                for v in (self._product(pid).get("variants") or []) if v.get("vid")]

    def get_product_info(self, vid: str) -> SupplierProductInfo:
        ids = split_vid(vid)
        p = self._product(ids["vid_a"])
        row = next((v for v in (p.get("variants") or []) if str(v.get("vid")) == ids["vid_b"]), None)
        if row is None:
            raise SupplierError(f"CJ has no variant {ids['vid_b']} for product {ids['vid_a']} (removed or wrong id).", retryable=False)
        inv = row.get("inventories") or []
        src = (self.creds.get("from_country") or "").upper()
        stock = sum(int(i.get("totalInventory") or 0) for i in inv if not src or str(i.get("countryCode", "")).upper() == src) if inv else 0
        return SupplierProductInfo(vid=vid, stock=stock, price=_dec(row.get("variantSellPrice") or p.get("sellPrice") or "0"))

    # ---- shipping ----------------------------------------------------------------------------
    def shipping_options(self, vid: str, quantity: int = 1, country: str = "", from_country: str = "") -> list[dict]:
        """[{code, fee, days}] cheapest first."""
        v = split_vid(vid)["vid_b"] or vid
        d = self._call("POST", "/logistic/freightCalculate", body={"startCountryCode": (from_country or self.creds.get("from_country") or "CN").upper(),
                                                                    "endCountryCode": (country or self.creds.get("ship_to_country") or "US").upper(),
                                                                    "products": [{"quantity": max(1, int(quantity)), "vid": v}]})
        rows = d.get("data") or []
        out = [{"code": str(r.get("logisticName")), "fee": _dec(r.get("logisticPrice")), "days": _days(r.get("logisticAging"))} for r in rows if r.get("logisticName")]
        return sorted(out, key=lambda o: (o["fee"], o["days"] or 999))

    # ---- orders ------------------------------------------------------------------------------
    def create_order(self, req: SupplierOrderRequest) -> SupplierOrderResult:
        if not req.lines:
            raise SupplierError("Order has no items", retryable=False)
        ship = (req.logistic_name or "").strip()
        if ship.upper() in ("", "AUTO"):
            opts = self.shipping_options(req.lines[0].vid, req.lines[0].quantity, req.country_code, req.from_country_code)
            if not opts:
                raise SupplierError(f"CJ offers no shipping method to {req.country_code or '?'} for this product: the order is held for you.", retryable=False)
            ship = opts[0]["code"]
        body = {"orderNumber": req.order_number, "shippingCountryCode": req.country_code, "shippingCountry": req.country or req.country_code, "shippingProvince": req.province,
                "shippingCity": req.city, "shippingCustomerName": req.customer_name, "shippingAddress": req.address1, "shippingAddress2": req.address2, "shippingZip": req.zip_code,
                "shippingPhone": req.phone, "email": req.email, "logisticName": ship, "fromCountryCode": (req.from_country_code or self.creds.get("from_country") or "CN").upper(),
                "remark": req.remark, "platform": "CheapeDeal", "payType": int(self.creds.get("pay_type") or 2),
                "products": [{"vid": split_vid(ln.vid)["vid_b"] or ln.vid, "quantity": ln.quantity} for ln in req.lines]}
        body = {k: v for k, v in body.items() if v not in ("", None)}
        d = self._call("POST", "/shopping/order/createOrderV2", body=body, ambiguous_on_timeout=True)
        x = d.get("data") or {}
        oid = x.get("orderId")
        if not oid:
            raise SupplierError(f"CJ accepted the call but returned no order id: {str(d)[:200]}", ambiguous=True)
        return SupplierOrderResult(supplier_order_id=str(oid), status="confirmed", raw=d)

    def find_order(self, order_number: str) -> Optional[SupplierOrderResult]:
        return None

    def get_balance(self) -> Decimal:
        """CJ wallet balance in USD (orders are paid from it). Path from CJ's public API list; the answer shape is read tolerantly. Not run live."""
        d = self._call("GET", "/shopping/pay/getBalance")
        x = d.get("data")
        if isinstance(x, dict):
            x = x.get("amount", x.get("balance", x.get("totalAmount")))
        try:
            return Decimal(str(x))
        except Exception:
            raise SupplierError("CJ ne balance ka jawab alag shape mein diya", retryable=False)

    def get_order_info(self, supplier_order_id: str) -> SupplierOrderInfo:
        d = self._call("GET", "/shopping/order/getOrderDetail", params={"orderId": supplier_order_id})
        x = d.get("data") or {}
        raw = str(x.get("orderStatus") or "")
        track = str(x.get("trackNumber") or "")
        status = STATUS.get(raw.upper(), "shipped" if track else "pending")
        return SupplierOrderInfo(supplier_order_id=supplier_order_id, status=status, raw_status=raw, tracking_number=track, carrier=str(x.get("logisticName") or ""), raw=d)

    # ---- checks ------------------------------------------------------------------------------
    def test_connection(self) -> str:
        t = self.ensure_token()
        left = int((t.get("refresh_expires_at", 0) - time.time()) // 86400)
        pid = (self.creds.get("test_product_id") or "").strip()
        if pid:
            self._product(pid)
            return f"CJ API key works; product {pid} was read successfully."
        n = len(self.list_catalog("", 1, 1))
        return f"CJ API key works ({n} product mila). Token apne aap renew hota rahega." + (f" (token ~{left} din)" if left > 0 else "")

    def checklist(self) -> list[dict]:
        out: list[dict] = []

        def add(step, ok, detail):
            out.append({"step": step, "ok": ok, "detail": detail})
            return ok
        if not add("API key", bool(self.creds.get("api_key")), "Saved hai" if self.creds.get("api_key") else "API key khali hai"):
            return out
        try:
            self.ensure_token()
            add("Token", True, "CJ ne key mani, token mil gaya (apne aap renew hota hai)")
        except SupplierError as e:
            add("Token", False, str(e))
            return out
        rows: list = []
        try:
            rows = self.list_catalog("phone case", 1, 3)
            add("Product search", bool(rows), f"{len(rows)} product mile" if rows else "Search se kuch nahi aaya")
        except SupplierError as e:
            add("Product search", False, str(e))
        pid = (self.creds.get("test_product_id") or "").strip() or (rows[0].supplier_product_id if rows else "")
        if pid:
            try:
                vs = self.variants_of(pid)
                add("Variants", bool(vs), f"{len(vs)} variant mile" if vs else "Product mila par variant nahi")
                if vs:
                    opts = self.shipping_options(vs[0]["vid"], 1)
                    add("Shipping options", bool(opts), (f"{len(opts)} tareeke; sabse sasta {opts[0]['code']} (${opts[0]['fee']})" if opts else "Is desh ke liye shipping nahi mili"))
            except SupplierError as e:
                add("Variants / Shipping", False, str(e))
        return out


def _days(v) -> int:
    import re
    nums = [int(n) for n in re.findall(r"\d+", str(v or ""))]
    return max(nums) if nums else 0
