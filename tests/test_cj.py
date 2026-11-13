from __future__ import annotations

import json
import time
from decimal import Decimal

import httpx
import pytest

from app.adapters.base import SupplierError, SupplierLine, SupplierOrderRequest
from app.connectors.cj import CJSupplier
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import login, post

CALLS: list = []


def _env(data, code=200, msg="Success"):
    return httpx.Response(200, json={"code": code, "result": code == 200, "message": msg, "data": data})


def _server(overrides=None):
    CALLS.clear()

    def handler(r: httpx.Request):
        body = json.loads(r.content) if r.content else {}
        CALLS.append((r.method, r.url.path.replace("/api2.0/v1", ""), dict(r.url.params), body, r.headers.get("CJ-Access-Token", "")))
        path = r.url.path.replace("/api2.0/v1", "")
        if overrides and path in overrides:
            return overrides[path](r, body)
        if path == "/authentication/getAccessToken":
            return _env({"accessToken": "AT1", "refreshToken": "RT1", "accessTokenExpiryDate": "2099-01-01T00:00:00+00:00", "refreshTokenExpiryDate": "2099-06-01T00:00:00+00:00"}) if body.get("apiKey") == "KEY" \
                else _env(None, 1600001, "Invalid apiKey")
        if path == "/product/listV2":
            return _env({"content": [{"productList": [{"id": "P1", "nameEn": "Phone Case", "sku": "CJ-1", "bigImage": "http://i/1.jpg", "sellPrice": "1.20 -- 2.40", "nowPrice": "1.20"}]}]})
        if path == "/product/query":
            return _env({"pid": "P1", "sellPrice": "1.20", "variants": [
                {"vid": "V1", "variantSellPrice": 1.5, "variantNameEn": "Red", "inventories": [{"countryCode": "CN", "totalInventory": 7}, {"countryCode": "US", "totalInventory": 3}]},
                {"vid": "V2", "variantSellPrice": 1.9, "inventories": []}]})
        if path == "/logistic/freightCalculate":
            return _env([{"logisticName": "CJPacket Fast", "logisticPrice": 6.0, "logisticAging": "5-8"}, {"logisticName": "CJPacket Ordinary", "logisticPrice": 2.5, "logisticAging": "15-25"}])
        if path == "/shopping/order/createOrderV2":
            return _env({"orderId": "CJ900", "orderStatus": "CREATED"})
        if path == "/shopping/order/getOrderDetail":
            return _env({"orderId": "CJ900", "orderStatus": "SHIPPED", "trackNumber": "TRK1", "logisticName": "CJPacket Ordinary"})
        return httpx.Response(404, json={})
    return handler


def _sup(overrides=None, **creds):
    store = {}
    s = CJSupplier({"api_key": "KEY", "from_country": "CN", "ship_to_country": "IN", **creds}, token_load=lambda: dict(store), token_save=store.update,
                   client=httpx.Client(transport=httpx.MockTransport(_server(overrides))))
    s.sleep = lambda x: None
    return s, store


def _req(ship=""):
    return SupplierOrderRequest(order_number="CD-1", customer_name="Asha", email="a@x.com", phone="999", address1="1 MG Road", address2="", city="Pune", province="MH",
                                zip_code="411001", country_code="IN", country="India", logistic_name=ship, from_country_code="CN", lines=[SupplierLine(vid="P1|V1", sku="CJ-1", quantity=2)])


def test_token_is_fetched_once_with_the_api_key_and_sent_as_header():
    s, store = _sup()
    items = s.list_catalog("case", 1, 5)
    s.list_catalog("case", 2, 5)
    assert [c[1] for c in CALLS].count("/authentication/getAccessToken") == 1  # cached, not asked again
    assert CALLS[0][3] == {"apiKey": "KEY"} and CALLS[1][4] == "AT1" and store["access_token"] == "AT1"
    assert items[0].supplier_product_id == "P1" and items[0].cost == Decimal("1.20") and CALLS[1][2]["keyWord"] == "case"


def test_wrong_key_gives_a_clear_hinglish_error():
    s, _ = _sup(api_key="BAD")
    with pytest.raises(SupplierError, match="API key") as e:
        s.test_connection()
    assert not e.value.retryable


def test_expired_token_renews_by_refresh_then_falls_back_to_the_key():
    s, store = _sup()
    store.update({"access_token": "OLD", "refresh_token": "R", "access_expires_at": time.time() + 60, "refresh_expires_at": time.time() + 10 * 86400})
    s2 = CJSupplier({"api_key": "KEY"}, token_load=lambda: dict(store), token_save=store.update, client=httpx.Client(transport=httpx.MockTransport(
        _server({"/authentication/refreshAccessToken": lambda r, b: _env({"accessToken": "AT2", "refreshToken": "R2"})}))))
    assert s2.ensure_token()["access_token"] == "AT2" and store["refresh_token"] == "R2"
    store.update({"access_token": "OLD", "access_expires_at": 1, "refresh_expires_at": 1})  # everything expired: no human needed
    s3 = CJSupplier({"api_key": "KEY"}, token_load=lambda: dict(store), token_save=store.update, client=httpx.Client(transport=httpx.MockTransport(_server())))
    assert s3.ensure_token()["access_token"] == "AT1"


def test_variants_stock_price_and_shipping():
    s, _ = _sup()
    assert s.variants_of("P1")[0]["vid"] == "P1|V1"
    info = s.get_product_info("P1|V1")
    assert info.stock == 7 and info.price == Decimal("1.5")  # only the CN warehouse counts
    assert s.get_product_info("P1|V2").stock == 0
    with pytest.raises(SupplierError, match="no variant"):
        s.get_product_info("P1|NOPE")
    opts = s.shipping_options("P1|V1", 2, "IN")
    assert [o["code"] for o in opts] == ["CJPacket Ordinary", "CJPacket Fast"] and opts[0]["days"] == 25
    fc = next(c for c in CALLS if c[1] == "/logistic/freightCalculate")[3]
    assert fc == {"startCountryCode": "CN", "endCountryCode": "IN", "products": [{"quantity": 2, "vid": "V1"}]}


def test_order_uses_cheapest_shipping_on_auto_and_pays_from_balance():
    s, _ = _sup()
    for blank in ("", "AUTO"):
        CALLS.clear()
        assert s.create_order(_req(blank)).supplier_order_id == "CJ900"
        body = next(c for c in CALLS if c[1] == "/shopping/order/createOrderV2")[3]
        assert body["logisticName"] == "CJPacket Ordinary" and body["payType"] == 2 and body["products"] == [{"vid": "V1", "quantity": 2}]
        assert body["orderNumber"] == "CD-1" and body["shippingCountryCode"] == "IN" and "shippingAddress2" not in body
    CALLS.clear()
    s.create_order(_req("MyShip"))
    assert [c[1] for c in CALLS if "freight" in c[1]] == []
    oi = s.get_order_info("CJ900")
    assert oi.status == "shipped" and oi.tracking_number == "TRK1" and oi.carrier == "CJPacket Ordinary"


def test_refused_order_and_low_balance_are_final_errors():
    s, _ = _sup({"/shopping/order/createOrderV2": lambda r, b: _env(None, 1600300, "Insufficient balance")})
    with pytest.raises(SupplierError) as e:
        s.create_order(_req("X"))
    assert "wallet" in str(e.value) and not e.value.ambiguous and not e.value.retryable


def test_rate_limit_retries_once():
    n = {"c": 0}

    def limited(r, b):
        n["c"] += 1
        return _env(None, 429, "Too Many Requests") if n["c"] == 1 else _env({"content": []})
    s, _ = _sup({"/product/listV2": limited})
    assert s.list_catalog("x") == [] and n["c"] == 2


def test_checklist_and_one_box_connect(client):
    s, _ = _sup()
    got = {c["step"]: c["ok"] for c in s.checklist()}
    assert got == {"API key": True, "Token": True, "Product search": True, "Variants": True, "Shipping options": True}
    tok = login(client)
    assert "CJ Dropshipping" in client.get("/connect").text
    post(client, "/connect/cj/easy", tok, api_key="")
    from app.models import Connection
    with client.app.state.database.session() as db:
        assert db.query(Connection).filter(Connection.type == "cj_dropshipping").count() == 0
    post(client, "/connect/cj/easy", tok, api_key="CJUser@api@xyz")  # real network is not reachable here: saved, test fails, clearly
    with client.app.state.database.session() as db:
        row = db.query(Connection).filter(Connection.type == "cj_dropshipping").one()
        assert row.is_default and row.enabled and "CJUser" not in row.secrets_enc and row.last_status == "error"


def test_startup_connects_cj_from_the_env_key_and_disables_removed_types(client):
    from app.models import Connection
    from app.services import bootstrap
    mgr = client.app.state.connectors
    with client.app.state.database.session() as db:
        db.add(Connection(kind="supplier", type="aliexpress_ds", name="Old AE", enabled=True, is_default=True))
        db.commit()
        assert bootstrap.ensure_cj(db, mgr, "") == "no CJ_API_KEY"
        assert bootstrap.disable_removed_connections(db, mgr) == 1
        assert bootstrap.ensure_cj(db, mgr, "CJ1@api@abc") == "connected from CJ_API_KEY"
        assert bootstrap.ensure_cj(db, mgr, "CJ1@api@abc") == "already connected"
        cj = db.query(Connection).filter(Connection.type == "cj_dropshipping").one()
        old = db.query(Connection).filter(Connection.type == "aliexpress_ds").one()
        assert cj.is_default and cj.enabled and "abc" not in cj.secrets_enc and not old.enabled and not old.is_default and "removed" in old.last_error
        bootstrap.ensure_cj(db, mgr, "CJ1@api@NEWKEY")  # a changed key replaces the saved one, still one connection
        assert db.query(Connection).filter(Connection.type == "cj_dropshipping").count() == 1 and mgr.secrets_of(cj)["api_key"] == "CJ1@api@NEWKEY"
    tok = login(client)
    assert client.get("/connect").status_code == 200 and client.get(f"/connect/{old.id}/edit").status_code == 404  # no crash on the leftover row


def test_connection_test_inside_a_request_does_not_deadlock_sqlite_and_hides_tokens(client):
    """Regression: saving the CJ token from a second DB session while the request held the write lock gave 'database is locked'."""
    import json as _j
    import httpx
    from app.connectors import cj as CJ
    from app.connectors.manager import scrub
    tok = login(client)

    def handler(req: httpx.Request):
        if "getAccessToken" in req.url.path:
            return httpx.Response(200, json={"code": 200, "result": True, "data": {"accessToken": "API@CJ1@CJ:eyJ" + "a" * 60, "refreshToken": "r" * 30,
                                                                              "accessTokenExpiryDate": "2099-01-01T00:00:00+08:00", "refreshTokenExpiryDate": "2099-01-01T00:00:00+08:00"}})
        return httpx.Response(200, json={"code": 200, "result": True, "data": {"content": [], "list": [], "total": 0}})
    orig = CJ.CJSupplier.__init__

    def patched(self, creds, token_load=None, token_save=None, client=None):
        orig(self, creds, token_load=token_load, token_save=token_save, client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://developers.cjdropshipping.com/api2.0/v1"))
    CJ.CJSupplier.__init__ = patched
    try:
        r = post(client, "/connect/cj/easy", tok, api_key="CJ1@api@abc123def456")
    finally:
        CJ.CJSupplier.__init__ = orig
    assert r.status_code == 303
    from app.models import Setting
    with client.app.state.database.session() as db:
        assert "locked" not in "".join((c.last_error or "") for c in db.query(__import__("app.models", fromlist=["Connection"]).Connection).all())
        assert db.get(Setting, "cj_tokens:1") is not None  # the token was stored and committed
    assert "eyJ" not in scrub("boom API@CJ1@CJ:eyJ" + "a" * 60 + " [SQL: INSERT x]") and "[SQL" not in scrub("x [SQL: y]")
