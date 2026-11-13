from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.adapters.base import SupplierError, SupplierLine, SupplierOrderRequest
from app.connectors.custom_site import CustomSiteStore, sign_hex
from app.connectors.generic_rest import GenericRestSupplier
from app.connectors.manager import ConnectorManager
from app.connectors.registry import load_connector_files
from app.connectors.rest_utils import dig, render
from app.db import Database
from app.models import Connection
from app.secretbox import seal, unseal

SPEC = {
    "base_url": "https://sup.test/v1", "auth": {"type": "header", "name": "Authorization", "value": "Bearer {api_key}"},
    "catalog": {"path": "/products", "params": {"q": "{query}", "page": "{page}"}, "items_path": "data.items",
                "map": {"supplier_product_id": "id", "vid": "variants.0.id", "title": "name", "cost": "price", "stock": "stock", "image_url": "images.0"}},
    "product_info": {"path": "/variants/{vid}", "map": {"stock": "stock", "price": "price"}},
    "create_order": {"method": "POST", "path": "/orders", "id_path": "data.id",
                     "body": {"ref": "{order_number}", "who": {"name": "{name}"}, "items": "{lines}"}, "line_template": {"v": "{vid}", "q": "{quantity}"}},
    "order_info": {"path": "/orders/{id}", "status_path": "data.status", "tracking_path": "data.trk", "status_map": {"sent": "shipped"}},
}


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def req(order_number="CD-1"):
    return SupplierOrderRequest(order_number=order_number, customer_name="Asha", email="a@x.com", phone="1", address1="a", address2="", city="Pune",
                                province="MH", zip_code="4", country_code="IN", country="India", logistic_name="", from_country_code="CN",
                                lines=[SupplierLine(vid="V9", quantity=2)])


def test_dig_and_render():
    assert dig({"a": [{"b": 5}]}, "a.0.b") == 5 and dig({"a": 1}, "a.x", "d") == "d"
    assert render({"x": "{n}", "y": "n={n}", "z": ["{n}"]}, {"n": 3}) == {"x": 3, "y": "n=3", "z": [3]}
    assert render("{unknown}", {}) == "{unknown}"


def test_generic_rest_catalog_info_order_tracking():
    seen = {}

    def handler(r: httpx.Request):
        seen[r.url.path] = r
        assert r.headers["authorization"] == "Bearer KEY"
        if r.url.path == "/v1/products":
            assert r.url.params["q"] == "lamp"
            return httpx.Response(200, json={"data": {"items": [{"id": 7, "name": "Lamp", "price": "4.5", "stock": 9, "variants": [{"id": "V7"}], "images": ["http://i/1.png"]}]}})
        if r.url.path == "/v1/variants/V7":
            return httpx.Response(200, json={"stock": 3, "price": "5.25"})
        if r.url.path == "/v1/orders" and r.method == "POST":
            return httpx.Response(200, json={"data": {"id": "S-1"}})
        if r.url.path == "/v1/orders/S-1":
            return httpx.Response(200, json={"data": {"status": "sent", "trk": "TRK1"}})
        return httpx.Response(404)

    sup = GenericRestSupplier(SPEC, "KEY", client=client_for(handler))
    items = sup.list_catalog("lamp")
    assert items[0].vid == "V7" and items[0].cost == Decimal("4.5") and items[0].image_url.endswith("1.png")
    info = sup.get_product_info("V7")
    assert info.stock == 3 and info.price == Decimal("5.25")
    res = sup.create_order(req())
    assert res.supplier_order_id == "S-1"
    body = json.loads(seen["/v1/orders"].content)
    assert body == {"ref": "CD-1", "who": {"name": "Asha"}, "items": [{"v": "V9", "q": 2}]}
    oi = sup.get_order_info("S-1")
    assert oi.status == "shipped" and oi.tracking_number == "TRK1"


def test_generic_rest_errors_are_classified():
    def boom(r):
        raise httpx.ReadTimeout("slow")

    sup = GenericRestSupplier(SPEC, "K", client=client_for(boom))
    with pytest.raises(SupplierError) as e:
        sup.create_order(req())
    assert e.value.ambiguous and not e.value.retryable  # never retry a possibly-created order blindly
    with pytest.raises(SupplierError) as e:
        sup.get_product_info("V1")
    assert e.value.retryable and not e.value.ambiguous
    sup = GenericRestSupplier(SPEC, "K", client=client_for(lambda r: httpx.Response(429)))
    with pytest.raises(SupplierError) as e:
        sup.list_catalog()
    assert e.value.retryable
    sup = GenericRestSupplier(SPEC, "K", client=client_for(lambda r: httpx.Response(401, text="no")))
    with pytest.raises(SupplierError) as e:
        sup.list_catalog()
    assert not e.value.retryable


def test_custom_site_publish_webhook_and_order():
    calls = []

    def handler(r: httpx.Request):
        calls.append((r.method, r.url.path, r.headers.get("x-key")))
        return httpx.Response(200, json={"id": "site-55", "url": "https://my.site/p/55"})

    store = CustomSiteStore({"base_url": "https://my.site", "auth_header_name": "X-Key", "auth_header_value": "{api_key}"}, "sekret", "whsec",
                            client=client_for(handler))
    from app.adapters.base import ProductPayload
    rid, url = store.publish_product(ProductPayload(title="Lamp", price=Decimal("9.9"), internal_id="12"))
    assert (rid, url) == ("site-55", "https://my.site/p/55") and calls[0] == ("POST", "/api/products", "sekret")
    raw = json.dumps({"id": "A1", "status": "paid", "total": "30", "currency": "USD", "customer": {"name": "Ravi", "email": "r@x.com"},
                      "shipping": {"address1": "x", "city": "Pune", "zip": "4", "country_code": "IN"},
                      "items": [{"product_id": "site-55", "title": "Lamp", "quantity": 2, "unit_price": "15"}]}).encode()
    sig = hmac.new(b"whsec", raw, hashlib.sha256).hexdigest()
    assert store.verify_webhook(raw, sig) and store.verify_webhook(raw, "sha256=" + sig)
    assert not store.verify_webhook(raw, "00" * 32) and not store.verify_webhook(raw, "")
    n = store.normalize_order(json.loads(raw))
    assert n.paid and n.items[0].store_product_id == "site-55" and n.items[0].quantity == 2 and n.country == "India"
    assert sign_hex("whsec", raw) == sig


def test_secretbox_roundtrip_and_wrong_key():
    t = seal("k" * 32, {"api_key": "abc"})
    assert "abc" not in t and unseal("k" * 32, t) == {"api_key": "abc"}
    assert unseal("z" * 32, t) == {}  # changed SECRET_KEY: unreadable but no crash
    assert unseal("k" * 32, "") == {}


GOOD = '''
from app.connectors.sdk import StoreConnector, Field
class Connector(StoreConnector):
    key = "acme"
    label = "Acme"
    fields = [Field("api_key", "Key", secret=True)]
'''


def test_connector_files_discovery_and_bad_files(tmp_path):
    (tmp_path / "acme.py").write_text(GOOD)
    (tmp_path / "broken.py").write_text("def x(:\n")
    (tmp_path / "empty.py").write_text("x = 1\n")
    (tmp_path / "_private.py").write_text(GOOD.replace("acme", "hidden"))
    types, errors = load_connector_files(tmp_path)
    assert [t.key for t in types] == ["file:acme"] and types[0].kind == "store" and types[0].file == "acme.py"
    assert set(errors) == {"broken.py", "empty.py"}


def test_manager_save_test_build_and_routing(tmp_path, cfg):
    (tmp_path / "acme.py").write_text(GOOD)
    database = Database(cfg.database_url)
    database.drop_all()
    database.create_all()
    m = ConnectorManager(database, cfg.secret_key, tmp_path)
    assert m.type("file:acme") and m.type("cj_dropshipping") and m.type("custom_site")
    with database.session() as db:
        t = m.type("mock_supplier")
        c = m.save(db, conn=None, kind="supplier", type_key="mock_supplier", name="Demo", cfg={}, sec={})
        ok, msg = m.test(db, c)
        assert ok and c.last_status == "ok"
        cid = c.id
        k = m.type("file:acme")
        c2 = m.save(db, conn=None, kind="store", type_key="file:acme", name="Acme", cfg={}, sec={"api_key": "SECRET-123"})
        assert "SECRET-123" not in c2.secrets_enc and m.secrets_of(c2) == {"api_key": "SECRET-123"}
        assert m.missing(k, {}, {}) == ["Key"]
        # blank secret on edit keeps the stored one
        cfg2, sec2 = m.split_form(k, {"api_key": ""}, c2)
        assert sec2 == {"api_key": "SECRET-123"}
    fallback = object()
    assert m.supplier(None, fallback) is fallback  # no default connection -> built-in
    a1, a2 = m.supplier(cid, fallback), m.supplier(cid, fallback)
    assert a1 is a2 and a1 is not fallback  # cached
    with database.session() as db:
        db.get(Connection, cid).enabled = False
    with pytest.raises(SupplierError):
        m.supplier(cid, fallback)  # a switched-off supplier must never silently fall back
    with pytest.raises(SupplierError):
        m.supplier(9999, fallback)


def test_add_missing_columns_upgrades_an_old_database(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    d = Database(url)
    with d.engine.begin() as c:  # an "old v1" products table without the new columns
        c.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY, store_product_id VARCHAR(64), sku VARCHAR(64), title VARCHAR(255), "
                       "supplier_vid VARCHAR(128), supplier_price NUMERIC, selling_price NUMERIC, min_margin_pct NUMERIC, stock INTEGER, "
                       "active BOOLEAN, auto_hidden BOOLEAN, manual_override BOOLEAN, hidden_reason VARCHAR(255), last_synced_at DATETIME, updated_at DATETIME)"))
        c.execute(text("INSERT INTO products (id, store_product_id, title, stock, active) VALUES (1, 'P1', 'Old lamp', 3, 1)"))
    d.create_all()
    with d.session() as s:
        from app.models import Product
        p = s.get(Product, 1)
        assert p.title == "Old lamp" and p.listing_status == "listed" and p.source == "manual" and p.supplier_connection_id is None


def test_supplier_connector_files_are_refused_builtin_suppliers_only(tmp_path):
    (tmp_path / "sup.py").write_text(GOOD.replace("StoreConnector", "SupplierConnector"))
    types, errors = load_connector_files(tmp_path)
    assert types == [] and "Supplier connector files are switched off" in errors["sup.py"]
    from app.connectors.registry import BUILTIN
    assert sorted(t.key for t in BUILTIN if t.kind == "supplier") == ["cj_dropshipping", "mock_supplier"]
