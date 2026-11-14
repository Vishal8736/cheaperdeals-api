"""Connector types: built-in ones plus any connector FILE found in the connectors folder."""
from __future__ import annotations

import importlib.util
import inspect
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..adapters.mock_store import MockStore
from ..adapters.mock_supplier import MockSupplier
from ..adapters.woocommerce import WooCommerceStore
from ..adapters.shopify import ShopifyStore
from .custom_site import CustomSiteStore
from .sdk import Field, StoreConnector, SupplierConnector

log = logging.getLogger("cheapedeal.connectors")


@dataclass
class ConnectorType:
    key: str
    kind: str  # supplier | store
    label: str
    description: str
    fields: list[Field]
    factory: Callable[[dict, dict, dict], Any]  # (config, secrets, ctx) -> adapter
    source: str = "built-in"  # built-in | file
    file: str = ""
    program: object = None  # v18: the affiliate / POD program this type belongs to (connectors/programs.py), None for the older types


def _woo(cfg: dict, sec: dict, ctx: dict):
    return WooCommerceStore(cfg["base_url"], sec.get("consumer_key", ""), sec.get("consumer_secret", ""), sec.get("webhook_secret", ""))


def _custom_site(cfg: dict, sec: dict, ctx: dict):
    return CustomSiteStore(cfg, sec.get("api_key", ""), sec.get("webhook_secret", ""))


BUILTIN: list[ConnectorType] = [
    ConnectorType("mock_supplier", "supplier", "Demo supplier (nakli, sirf practice ke liye)", "Nakli supplier, 12 demo products. Asli kaam CJ Dropshipping se hota hai.", [],
                  lambda c, s, x: MockSupplier()),
    ConnectorType("woocommerce", "store", "WooCommerce store", "Receive paid orders from, and list products on, a WooCommerce site.",
                  [Field("base_url", "Store URL", placeholder="https://myshop.com"),
                   Field("consumer_key", "Consumer key", secret=True), Field("consumer_secret", "Consumer secret", secret=True),
                   Field("webhook_secret", "Webhook secret", secret=True, help="The same secret you set in the WooCommerce webhook")],
                  _woo),
    ConnectorType("custom_site", "store", "My own website (API + key)",
                  "List products on your own website and receive its orders. Your site needs one endpoint that accepts a product.",
                  [Field("base_url", "Website URL", placeholder="https://mysite.com"),
                   Field("api_key", "API key your site expects", secret=True, required=False),
                   Field("auth_header_name", "Key header name", required=False, default="Authorization", help="Leave empty if your site needs no key"),
                   Field("auth_header_value", "Key header value", required=False, default="Bearer {api_key}"),
                   Field("publish_path", "Add-product path", required=False, default="/api/products", help="We POST each product here as JSON"),
                   Field("update_path", "Update-product path", required=False, placeholder="/api/products/{remote_id}", help="Optional. For price/visibility changes"),
                   Field("order_update_path", "Order-update path", required=False, placeholder="/api/orders/{order_id}", help="Optional. Tracking notes go here"),
                   Field("webhook_secret", "Webhook secret", secret=True, required=False, help="Your site signs paid-order calls with this (HMAC-SHA256)")],
                  _custom_site),
    ConnectorType("mock_store", "store", "Demo store", "A fake store for trying the listing flow.", [], lambda c, s, x: MockStore()),
]


def _shopify(cfg: dict, sec: dict, ctx: dict):
    return ShopifyStore(cfg["shop"], sec.get("access_token", ""), sec.get("webhook_secret", ""), cfg.get("api_version") or "2024-10")


BUILTIN.insert(1, ConnectorType("shopify", "store", "Shopify store",
                                "Receive paid orders from, and list products on, a Shopify shop (custom app Admin API token).",
                                [Field("shop", "Shop address", placeholder="myshop.myshopify.com"),
                                 Field("access_token", "Admin API access token", secret=True),
                                 Field("webhook_secret", "Webhook signing secret", secret=True, required=False,
                                       help="The app's client secret. Needed to receive orders; point the 'Order payment' webhook at the address shown on the connection"),
                                 Field("api_version", "API version", required=False, default="2024-10")], _shopify))
def _cj(cfg: dict, sec: dict, ctx: dict):
    from .cj import CJSupplier
    return CJSupplier({**cfg, **sec}, token_load=ctx.get("token_load"), token_save=ctx.get("token_save"))


BUILTIN.insert(1, ConnectorType("cj_dropshipping", "supplier", "CJ Dropshipping (sirf API key, Authorize nahi)",
                                "Sabse aasaan supplier: CJ account se ek API key copy karo, bas. Login/authorize ka jhanjhat nahi, token apne aap renew hota hai. Order CJ wallet balance se pay hota hai.",
                                [Field("api_key", "CJ API key", secret=True, help="CJ account mein login karo > Authorization (API) page > 'Generate API Key'. Key aisi dikhti hai: CJUserNum@api@... Poori copy karo."),
                                 Field("from_country", "Kahan se ship ho (2 letter)", required=False, default="CN", help="CN = China warehouse (sabse zyada products). US/TH jaise warehouse bhi hote hain."),
                                 Field("ship_to_country", "Default customer desh (2 letter)", required=False, default="US", help="Shipping aur stock dekhne ke liye."),
                                 Field("test_product_id", "Test product id (optional)", required=False, help="CJ product ka pid, Test button isse product lookup bhi check karta hai.")], _cj))

_HELP = {
    ("shopify", "shop"): "Tumhare Shopify store ka address, jaise mystore.myshopify.com (https:// ke bina).",
    ("shopify", "access_token"): "Shopify admin > Settings > Apps > Develop apps > app banao, Admin API scopes (products, orders) do, install karo aur 'Admin API access token' (shpat_...) yahan daalo. Ye sirf ek baar dikhta hai.",
    ("shopify", "api_version"): "Shopify API ka version (jaise 2024-10). Khali chhodo to default.",
    ("woocommerce", "base_url"): "Tumhari WooCommerce site ka address, jaise https://mystore.com (aakhir mein / ke bina).",
    ("woocommerce", "consumer_key"): "WordPress admin > WooCommerce > Settings > Advanced > REST API > Add key (Read/Write). 'ck_...' wala key.",
    ("woocommerce", "consumer_secret"): "Wahi key banate time dikhne wala 'cs_...' secret. Sirf ek baar dikhta hai: turant copy karo.",
    ("custom_site", "base_url"): "Tumhari apni website ke API ka address, jaise https://mysite.com/api. Details: docs/CONNECTORS.md.",
    ("custom_site", "api_key"): "Tumhari website ne jo API key banayi wo. Request ke saath bheji jati hai.",
    ("custom_site", "auth_header_value"): "Agar tumhari site ko key kisi khaas header mein chahiye to uski poori value. Aam taur par khali.",
}


def _wrap_file_class(cls: type, kind: str, fname: str) -> ConnectorType:
    key = "file:" + cls.key

    def factory(cfg: dict, sec: dict, ctx: dict, _cls=cls):
        return _cls(cfg, sec)

    return ConnectorType(key, kind, cls.label or cls.key, cls.description or f"Connector file {fname}", list(cls.fields), factory,
                         source="file", file=fname)


def load_connector_files(folder: Path) -> tuple[list[ConnectorType], dict[str, str]]:
    """Import every *.py in folder (skips names starting with _ and the examples/ subfolder). Returns (types, {file: error})."""
    types: list[ConnectorType] = []
    errors: dict[str, str] = {}
    if not folder.is_dir():
        return types, errors
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"cd_connector_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            found = 0
            for _, cls in inspect.getmembers(mod, inspect.isclass):
                if cls.__module__ != mod.__name__:
                    continue
                kind = "supplier" if issubclass(cls, SupplierConnector) else "store" if issubclass(cls, StoreConnector) else ""
                if not kind:
                    continue
                if kind == "supplier":
                    raise ValueError("Supplier connector files are switched off: use the built-in supplier (CJ Dropshipping). Store connector files still work.")
                if not re.fullmatch(r"[a-z0-9_]{2,40}", getattr(cls, "key", "") or ""):
                    raise ValueError(f"class {cls.__name__} needs key = 'letters_digits_underscore' (2-40 chars)")
                types.append(_wrap_file_class(cls, kind, path.name))
                found += 1
            if not found:
                errors[path.name] = "No SupplierConnector or StoreConnector class found in this file"
        except Exception as e:  # a broken file must never break the app
            log.warning("connector file %s failed: %s", path.name, e)
            errors[path.name] = f"{type(e).__name__}: {e}"
    return types, errors


def _program_types() -> list[ConnectorType]:
    from . import affiliate_sources as AS, pod
    from .programs import PROGRAMS, fields_for

    def make(p):
        def factory(cfg: dict, sec: dict, ctx: dict, _p=p):
            prog = {"key": _p.key, "label": _p.label}
            if _p.key in ("aff_awin", "aff_impact", "aff_cj_affiliate") and not sec.get("feed_url"):
                from ..services.earnings import EarningsOnlySource  # credentials only, no feed: earnings sync, nothing to import
                return EarningsOnlySource(_p.key, cfg, sec, prog)
            return {"feed": AS.FeedSource, "amazon": AS.AmazonCreatorsSource, "ebay": AS.EbaySource}[_p.mode](cfg, sec, prog) if _p.mode in ("feed", "amazon", "ebay") \
                else (pod.PrintfulSupplier if _p.mode == "printful" else pod.PrintifySupplier)(cfg, sec)
        kind = "supplier" if p.mode in ("printful", "printify") else "affiliate"
        desc = f"{p.commission}. {p.howto}"
        return ConnectorType(p.key, kind, p.label, desc, fields_for(p), factory, program=p)
    return [make(p) for p in PROGRAMS]


BUILTIN.extend(_program_types())

for _t in BUILTIN:
    for _f in _t.fields:
        if not _f.help and (_t.key, _f.key) in _HELP:
            _f.help = _HELP[(_t.key, _f.key)]
