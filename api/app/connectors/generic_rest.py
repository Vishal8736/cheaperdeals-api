"""Generic REST supplier: connect ANY company that has an HTTP/JSON API by describing it, no code.

The spec (JSON) says where to call and which fields to read. Placeholders look like {api_key}. Besides plain
key/bearer/basic auth it can sign requests the way some platforms ("iop", "aop", "top") require.
The ready-made presets in presets.py are just specs for this engine, so you can copy and edit any of them.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx

from ..adapters.base import (
    CatalogItem, SupplierError, SupplierOrderInfo, SupplierOrderRequest, SupplierOrderResult, SupplierProductInfo,
)
from . import signing
from .rest_utils import dig, render

DEFAULT_STATUS_MAP = {"pending": "pending", "paid": "confirmed", "confirmed": "confirmed", "processing": "confirmed",
                      "shipped": "shipped", "in_transit": "shipped", "delivered": "delivered", "completed": "delivered",
                      "cancelled": "cancelled", "canceled": "cancelled"}


def _dec(v, default="0") -> Decimal:
    try:
        return Decimal(str(v).split("--")[0].strip())
    except (InvalidOperation, AttributeError):
        return Decimal(default)


_UNRESOLVED = re.compile(r"^\{[A-Za-z0-9_\.]+\}$")


def drop_unresolved(params: dict) -> dict:
    """A param whose value is still a bare {placeholder} had no value (optional field left blank): do not send it."""
    return {k: v for k, v in params.items() if not (isinstance(v, str) and _UNRESOLVED.match(v))}


def split_vid(vid: str) -> dict:
    """Some suppliers need two ids (product + variant). We keep them in one field as 'PRODUCT|VARIANT'."""
    a, _, b = str(vid).partition("|")
    return {"vid": vid, "vid_a": a, "vid_b": b}


class GenericRestSupplier:
    name = "generic_rest"

    def __init__(self, spec: dict, api_key: str = "", client: Optional[httpx.Client] = None, creds: Optional[dict] = None):
        if not isinstance(spec, dict) or not spec.get("base_url"):
            raise ValueError("Spec needs at least a base_url")
        self.spec = spec
        self.creds = {k: v for k, v in (creds or {}).items() if v not in (None, "")}
        if api_key:
            self.creds["api_key"] = api_key
        self.api_key = self.creds.get("api_key", "")
        self.base = str(spec["base_url"]).rstrip("/")
        self.supports_lookup = bool(spec.get("find_order"))
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0))

    # ---- plumbing --------------------------------------------------------------------------
    def _send(self, ep: dict, ctx: dict):
        """Build and send one request. Returns the http response."""
        ctx = {**self.creds, **ctx}
        auth = self.spec.get("auth") or {}
        kind = auth.get("type", "none")
        params = drop_unresolved(render(ep.get("params") or {}, ctx))
        headers = {"Accept": "application/json"}
        headers.update({k: str(render(v, ctx)) for k, v in (self.spec.get("headers") or {}).items()})
        method = ep.get("method", "GET").upper()
        url = self.base + str(render(ep.get("path", ""), ctx))
        kw: dict[str, Any] = {}
        if ep.get("body") is not None:
            kw["json"] = render(ep["body"], ctx)

        if kind == "iop":  # generic signed REST (iop)
            sm = auth.get("sign_method", "sha256")
            signed = signing.iop_params(self.creds.get("app_key", ""), ep["api"], {k: v for k, v in params.items() if v not in (None, "")},
                                        self.creds.get("access_token", ""), sm)
            signed["sign"] = signing.sign_iop(self.creds.get("app_secret", ""), signed, sm)
            method, kw["data"] = "POST", signed
        elif kind == "aop":  # 1688
            url_path = f"param2/{ep.get('version', 1)}/{ep['namespace']}/{ep['api']}/{self.creds.get('app_key', '')}"
            p = {k: signing._s(v) for k, v in params.items() if v not in (None, "")}
            if self.creds.get("access_token"):
                p["access_token"] = self.creds["access_token"]
            p["_aop_signature"] = signing.sign_aop(self.creds.get("app_secret", ""), url_path, {k: v for k, v in p.items()})
            method, url, kw["data"] = "POST", f"{self.base}/openapi/{url_path}", p
        elif kind == "top":  # Taobao
            p = {"method": ep["api"], "app_key": self.creds.get("app_key", ""), "timestamp": signing.top_timestamp(), "format": "json",
                 "v": "2.0", "sign_method": "md5", **{k: signing._s(v) for k, v in params.items() if v not in (None, "")}}
            if self.creds.get("access_token"):
                p["session"] = self.creds["access_token"]
            p["sign"] = signing.sign_top(self.creds.get("app_secret", ""), p)
            method, kw["data"] = "POST", p
        else:
            if kind == "header":
                headers[auth.get("name", "Authorization")] = str(render(auth.get("value", "{api_key}"), ctx))
            elif kind == "bearer":
                headers["Authorization"] = f"Bearer {self.api_key}"
            elif kind == "query":
                params[auth.get("name", "api_key")] = self.api_key
            elif kind == "basic":
                kw["auth"] = (self.api_key, str(auth.get("password", "")))
            kw["params"] = params or None
        return self._client.request(method, url, headers=headers, **kw)

    def _call(self, ep: dict, ctx: dict, *, ambiguous_on_timeout: bool = False):
        try:
            r = self._send(ep, ctx)
        except httpx.TimeoutException as e:
            raise SupplierError(f"Supplier timed out: {e}", ambiguous=ambiguous_on_timeout, retryable=not ambiguous_on_timeout)
        except httpx.HTTPError as e:
            raise SupplierError(f"Supplier unreachable: {e}", retryable=True)
        if r.status_code == 429 or r.status_code >= 500:
            raise SupplierError(f"Supplier HTTP {r.status_code}", retryable=True, status_code=r.status_code,
                                ambiguous=ambiguous_on_timeout and r.status_code >= 500)
        if r.status_code >= 400:
            raise SupplierError(f"Supplier HTTP {r.status_code}: {r.text[:200]}", status_code=r.status_code)
        try:
            data = r.json()
        except ValueError:
            raise SupplierError("Supplier did not return JSON")
        err = self.spec.get("errors") or {}
        if err.get("path") and dig(data, err["path"]):  # many APIs say HTTP 200 with an error body
            msg = dig(data, err.get("message_path", err["path"]), "") or dig(data, err["path"])
            raise SupplierError(f"Supplier error: {str(msg)[:250]}")
        return data

    def _need(self, name: str) -> dict:
        ep = self.spec.get(name)
        if not isinstance(ep, dict):
            raise SupplierError(f"This connection has no '{name}' section in its spec")
        return ep

    @staticmethod
    def _pick(data, rule, ctx: dict):
        """A mapping value: a dotted path, or {"list_path","match":{"field","equals"},"value"} to read one row of a list."""
        if isinstance(rule, dict) and "list_path" in rule:
            rows = dig(data, rule["list_path"], [])
            if isinstance(rows, dict):
                rows = [rows]
            m = rule.get("match") or {}
            want = str(render(m.get("equals", ""), ctx)) if m else ""
            row = next((r for r in rows if not want or str(dig(r, m.get("field", ""), "")) == want), None) if isinstance(rows, list) else None
            return dig(row, rule.get("value", "")) if row is not None else None
        return dig(data, rule) if rule else None

    # ---- contract --------------------------------------------------------------------------
    def list_catalog(self, query: str = "", page: int = 1, page_size: int = 20, section: str = "catalog") -> list[CatalogItem]:
        ep = self._need(section)
        data = self._call(ep, {"query": query, "page": page, "limit": page_size})
        rows = dig(data, ep.get("items_path", ""), [])
        if isinstance(rows, dict):
            rows = [rows]
        m = ep.get("map") or {}

        def g(key: str, default: str, empty=""):
            path = m.get(key, default) if key in m else default
            return empty if path is None else path

        out = []
        for r in rows if isinstance(rows, list) else []:
            title = dig(r, g("title", "title"), "")
            if not title:
                continue
            pid = str(dig(r, g("supplier_product_id", "id"), ""))
            vpath = m.get("vid", m.get("supplier_product_id", "id")) if "vid" not in m or m["vid"] is not None else None
            out.append(CatalogItem(
                supplier_product_id=pid, vid=str(dig(r, vpath, pid)) if vpath is not None else "",
                title=str(title), cost=_dec(dig(r, g("cost", "price"), "0")),
                stock=int(_dec(dig(r, m["stock"], "0"))) if m.get("stock") else 0,
                sku=str(dig(r, g("sku", "sku"), "") or ""), image_url=str(dig(r, g("image_url", "image"), "") or ""),
                description=str(dig(r, g("description", "description"), "") or ""), category=str(dig(r, g("category", "category"), "") or ""),
            ))
        return out

    def variants_of(self, pid: str) -> list[dict]:
        """For suppliers whose list has products but ordering needs a variant: ask for the variants of one product."""
        ep = self.spec.get("variants")
        if not ep:
            return []
        data = self._call(ep, {"pid": pid, "product_id": pid})
        rows = dig(data, ep.get("items_path", ""), [])
        rows = [rows] if isinstance(rows, dict) else rows
        out = []
        for r in rows if isinstance(rows, list) else []:
            vid = str(render(ep.get("vid_template", "{id}"), {**r, "pid": pid}))
            out.append({"vid": vid, "variantSellPrice": dig(r, ep.get("price_path", "price"))})
        return out

    def get_product_info(self, vid: str) -> SupplierProductInfo:
        ep = self._need("product_info")
        ctx = split_vid(vid)
        data = self._call(ep, ctx)
        m = ep.get("map") or {}
        stock, price = self._pick(data, m.get("stock", "stock"), ctx), self._pick(data, m.get("price", "price"), ctx)
        if stock is None or price is None:
            raise SupplierError("product_info response has no stock/price at the mapped paths")
        return SupplierProductInfo(vid=vid, stock=int(_dec(stock)), price=_dec(price))

    def create_order(self, req: SupplierOrderRequest) -> SupplierOrderResult:
        ep = self._need("create_order")
        base_ctx = {"order_number": req.order_number, "name": req.customer_name, "email": req.email, "phone": req.phone,
                    "address1": req.address1, "address2": req.address2, "city": req.city, "state": req.province, "zip": req.zip_code,
                    "country_code": req.country_code, "country": req.country, "shipping_method": req.logistic_name,
                    "from_country_code": req.from_country_code, "remark": req.remark}
        lt = ep.get("line_template") or {"vid": "{vid}", "quantity": "{quantity}"}
        lines = [render(lt, {**base_ctx, **split_vid(l.vid), "sku": l.sku, "quantity": l.quantity}) for l in req.lines]
        data = self._call(ep, {**base_ctx, "lines": lines}, ambiguous_on_timeout=True)
        oid = dig(data, ep.get("id_path", "id"))
        if not oid:
            raise SupplierError(f"Supplier accepted the call but returned no order id: {str(data)[:200]}", ambiguous=True)
        return SupplierOrderResult(supplier_order_id=str(oid), status="confirmed", raw=data if isinstance(data, dict) else {})

    def find_order(self, order_number: str) -> Optional[SupplierOrderResult]:
        ep = self.spec.get("find_order")
        if not ep:
            return None
        try:
            data = self._call(ep, {"order_number": order_number})
        except SupplierError as e:
            if e.status_code == 404:
                return None
            raise
        oid = dig(data, ep.get("id_path", "id"))
        return SupplierOrderResult(supplier_order_id=str(oid), status="confirmed") if oid else None

    def get_order_info(self, supplier_order_id: str) -> SupplierOrderInfo:
        ep = self._need("order_info")
        data = self._call(ep, {"id": supplier_order_id, "order_id": supplier_order_id})
        raw = str(dig(data, ep.get("status_path", "status"), "") or "")
        smap = {**DEFAULT_STATUS_MAP, **{k.lower(): v for k, v in (ep.get("status_map") or {}).items()}}
        track = str(dig(data, ep.get("tracking_path", "tracking_number"), "") or "")
        status = smap.get(raw.lower(), "shipped" if track else "pending")
        return SupplierOrderInfo(supplier_order_id=supplier_order_id, status=status, raw_status=raw, tracking_number=track,
                                 carrier=str(dig(data, ep.get("carrier_path", "carrier"), "") or ""), raw=data if isinstance(data, dict) else {})

    def test_connection(self) -> str:
        ep = self.spec.get("test") or self.spec.get("catalog")
        if not ep:
            raise SupplierError("Add a 'test' or 'catalog' section to the spec so the connection can be checked")
        self._call(ep, {"query": "", "page": 1, "limit": 1})
        return "The company's API answered."


def parse_spec(text: str) -> dict:
    try:
        spec = json.loads(text)
    except ValueError as e:
        raise ValueError(f"The spec is not valid JSON: {e}")
    if not isinstance(spec, dict) or not spec.get("base_url"):
        raise ValueError("The spec must be a JSON object with a base_url")
    return spec
