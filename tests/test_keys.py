"""API Keys page, permanent CJ key, CJ options, keep-alive ping; crypto and demo world are gone."""
from __future__ import annotations

import httpx
import pytest

from app.models import Connection, Setting
from app.services import bootstrap, keepalive, shopcfg
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import login, post


def raw(client, key):
    with client.app.state.database.SessionLocal() as db:
        s = db.get(Setting, key)
        return s.value if s else None


def cj_handler(req: httpx.Request) -> httpx.Response:
    if "getAccessToken" in req.url.path:
        return httpx.Response(200, json={"code": 200, "result": True, "data": {"accessToken": "T", "refreshToken": "R", "accessTokenExpiryDate": "2099-01-01T00:00:00+08:00", "refreshTokenExpiryDate": "2099-01-01T00:00:00+08:00"}})
    return httpx.Response(200, json={"code": 200, "result": True, "data": {"pageNum": 1, "list": []}})


@pytest.fixture(autouse=True)
def fake():
    shopcfg.HTTP = httpx.Client(transport=httpx.MockTransport(cj_handler))
    yield
    shopcfg.HTTP = None


def test_keys_page_needs_login_and_admin(client):
    assert client.get("/keys").status_code == 303


def test_cj_key_is_saved_sealed_and_options_appear_only_after_it_works(client, monkeypatch):
    tok = login(client)
    html = client.get("/keys").text
    assert "CJ ke options" not in html  # nothing to configure before CJ is connected
    monkeypatch.setattr("app.connectors.manager.ConnectorManager.test", lambda self, db, conn: (setattr(conn, "last_status", "ok") or True, "ok"))
    post(client, "/keys/cj", tok, api_key="CJ1@api@SECRETKEY")
    html = client.get("/keys").text
    assert "SECRETKEY" not in html and "CJ ke options" in html
    assert raw(client, "cj_key_source") == "ui"
    with client.app.state.database.SessionLocal() as db:
        conn = db.query(Connection).filter(Connection.type == "cj_dropshipping").one()
        assert conn.is_default and "SECRETKEY" not in (conn.config_json or "")


def test_ui_key_is_permanent_env_cannot_override_it(client, monkeypatch):
    tok = login(client)
    monkeypatch.setattr("app.connectors.manager.ConnectorManager.test", lambda self, db, conn: (True, "ok"))
    post(client, "/keys/cj", tok, api_key="CJ1@api@UIKEY")
    mgr = client.app.state.connectors
    with client.app.state.database.SessionLocal() as db:
        assert bootstrap.ensure_cj(db, mgr, "CJ1@api@ENVKEY").startswith("kept")
        conn = db.query(Connection).filter(Connection.type == "cj_dropshipping").one()
        assert mgr.secrets_of(conn)["api_key"] == "CJ1@api@UIKEY"


def test_cj_options_validate_and_save(client, monkeypatch):
    tok = login(client)
    monkeypatch.setattr("app.connectors.manager.ConnectorManager.test", lambda self, db, conn: (True, "ok"))
    post(client, "/keys/cj", tok, api_key="CJ1@api@K")
    post(client, "/keys/cj-config", tok, default_logistic_name="", from_country_code="XYZ", markup_pct="abc", min_margin_pct="20", daily_order_cap="5")
    assert raw(client, "from_country_code") != "XYZ"
    post(client, "/keys/cj-config", tok, default_logistic_name="", from_country_code="cn", markup_pct="70", min_margin_pct="25", daily_order_cap="9")
    assert raw(client, "markup_pct") == "70" and raw(client, "default_logistic_name") == "AUTO" and raw(client, "from_country_code") == "CN"


def test_keys_form_does_not_switch_live_off(client):
    tok = login(client)
    with client.app.state.database.SessionLocal() as db:
        from app.services import settings_service as S
        S.set_value(db, "host_live", "true")
        db.commit()
    post(client, "/website/connect/save-all", tok, vercel="vercel-token-long", next="/keys")
    assert raw(client, "host_live") == "true"


def test_ping_function_reports_ok_and_failure(monkeypatch):
    def fake_get(url, **kw):
        return httpx.Response(200, text="ok") if "good" in url else (_ for _ in ()).throw(httpx.ConnectError("x"))
    monkeypatch.setattr(httpx, "get", fake_get)
    assert keepalive.ping("https://good.example.com")["ok"] and keepalive.LAST["url"] == "https://good.example.com/ping"
    bad = keepalive.ping("https://bad.example.com")
    assert not bad["ok"] and bad["error"] == "ConnectError"


def test_ping_button_pings_backend_and_site(client, monkeypatch):
    tok = login(client)
    seen = []
    monkeypatch.setattr(httpx, "get", lambda url, **kw: (seen.append(url), httpx.Response(200, text="ok"))[1])
    with client.app.state.database.SessionLocal() as db:
        from app.services import settings_service as S
        S.set_value(db, "host_render_url", "https://b.onrender.com")
        S.set_value(db, "host_vercel_url", "https://s.vercel.app")
        db.commit()
    post(client, "/keys/ping", tok)
    assert seen == ["https://b.onrender.com/ping", "https://s.vercel.app/api/ping"]
    assert "jag raha hai" in client.get("/keys").text


def test_crypto_and_demo_world_are_gone(client):
    tok = login(client)
    for url in ("/simulator", "/simulator/toggle"):
        assert client.get(url).status_code == 404
    assert client.post("/shop/btcpay/webhook", content=b"{}").status_code in (404, 405)
    html = client.get("/website").text.lower()
    assert "btc" not in html and "crypto" not in html and "demo duniya" not in client.get("/").text.lower()
    assert "Demo duniya" not in client.get("/setup").text


def test_connector_edit_page_shows_the_saved_connection_not_an_empty_form(client):
    """Regression: a duplicated route made /connect/<id>/edit render a blank new-connection form."""
    tok = login(client)
    with client.app.state.database.SessionLocal() as db:
        c = Connection(type="cj_dropshipping", kind="supplier", name="My CJ edit", enabled=True, config_json="{}")
        db.add(c)
        db.commit()
        cid = c.id
    html = client.get(f"/connect/{cid}/edit").text
    assert "My CJ edit" in html


def test_connected_supplier_is_tested_in_the_background_and_becomes_ready(client):
    """'CJ ko test se ready status par': no one should have to press Test after a restart."""
    from app.models import Connection
    from app.services import bootstrap
    tok = login(client)
    mgr = client.app.state.connectors
    db_ = client.app.state.database
    with db_.session() as s:
        ok, _ = bootstrap.set_cj_key(s, mgr, "CJ-KEY-X")
        c = s.query(Connection).filter(Connection.type == "cj_dropshipping").one()
        c.last_status, c.last_tested_at = "untested", None
        s.commit()
    calls = []
    mgr.test = lambda s, c: (calls.append(c.type), setattr(c, "last_status", "ok"), (True, "ok"))[2]
    assert bootstrap.verify_suppliers(db_, mgr) == 1 and calls == ["cj_dropshipping"]
    with db_.session() as s:
        assert s.query(Connection).filter(Connection.type == "cj_dropshipping").one().last_status == "ok"
