from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from app.adapters.woocommerce import sign
from app.config import Config
from app.main import create_app
from app.version import VERSION
from app.models import Order
from tests.conftest import db_url_for


@pytest.fixture
def web_cfg(tmp_path):
    return Config(app_env="test", database_url=db_url_for(tmp_path, "w"), secret_key="s" * 32, internal_api_key="i" * 32,
                  admin_username="admin", admin_password="correct horse", timezone="UTC", enable_dev_tools=True,
                  supplier_backend="mock", store_backend="mock")


@pytest.fixture
def client(web_cfg):
    app = create_app(web_cfg)
    app.state.database.drop_all()
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.state.database.drop_all()
    app.state.database.engine.dispose()


def login(c):
    r = c.post("/login", data={"username": "admin", "password": "correct horse"})
    assert r.status_code == 303
    return csrf(c)


def csrf(c):
    html = c.get("/").text
    m = re.search(r'X-CSRF-Token": "([^"]+)"', html)
    assert m, "csrf token missing from page"
    return m.group(1)


def post(c, url, tok, **data):
    return c.post(url, data={"csrf_token": tok, **data})


# ---- auth ----------------------------------------------------------------------------------
def test_pages_require_login(client):
    for url in ["/", "/orders", "/products", "/approvals", "/alerts", "/settings", "/orders/1"]:
        r = client.get(url)
        assert r.status_code == 303 and r.headers["location"] == "/login", url


def test_wrong_password_rejected_and_rate_limited(client):
    for _ in range(5):
        assert client.post("/login", data={"username": "admin", "password": "nope"}).status_code == 401
    assert client.post("/login", data={"username": "admin", "password": "correct horse"}).status_code == 429


def test_post_without_csrf_is_refused(client):
    login(client)
    assert client.post("/settings/kill", data={"state": "pause"}).status_code == 403
    assert client.post("/settings/kill", data={"state": "pause", "csrf_token": "wrong"}).status_code == 403


def test_logout_clears_session(client):
    tok = login(client)
    assert post(client, "/logout", tok).status_code == 303
    assert client.get("/").status_code == 303


# ---- pages render --------------------------------------------------------------------------
def test_all_pages_render_when_empty(client):
    login(client)
    for url in ["/", "/partials/dashboard", "/orders", "/orders/table", "/products", "/approvals", "/alerts", "/alerts?tab=audit",
                "/alerts?show=all", "/settings"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:300])
    assert "Everything is moving" in client.get("/").text
    assert "Rehearsal" in client.get("/").text  # safe default is visible


def test_unknown_order_is_404_page(client):
    login(client)
    assert client.get("/orders/999", headers={"accept": "text/html"}).status_code == 404


# ---- kill switch + dry run -----------------------------------------------------------------
def test_kill_switch_and_go_live_guard(client):
    tok = login(client)
    post(client, "/settings/kill", tok, state="pause", next="/settings")
    assert "ROKA HUA" in client.get("/").text
    post(client, "/settings/kill", tok, state="resume", next="/")
    assert "ROKA HUA" not in client.get("/").text
    post(client, "/settings/dry-run", tok, state="live")  # no shipping method yet -> refused
    assert "Rehearsal" in client.get("/").text


def test_open_redirect_blocked_on_kill_next(client):
    tok = login(client)
    r = post(client, "/settings/kill", tok, state="pause", next="//evil.example")
    assert r.headers["location"] == "/"


# ---- end to end with dev tools -------------------------------------------------------------
def test_simulated_order_flow_through_ui(client):
    tok = login(client)
    post(client, "/dev/seed", tok)
    assert "Desk Lamp" in client.get("/products").text
    r = post(client, "/dev/simulate", tok, qty="1")
    assert r.status_code == 303 and r.headers["location"].startswith("/orders/")
    detail = client.get(r.headers["location"])
    assert detail.status_code == 200 and "Rehearsal only" in detail.text and "With supplier" in detail.text


def test_big_order_waits_for_approval_then_approve(client):
    tok = login(client)
    post(client, "/dev/seed", tok)
    cfg = {"min_margin_pct": "20", "markup_pct": "60", "approval_threshold": "20", "daily_order_cap": "20", "max_retries": "5",
           "retry_base_minutes": "2", "default_logistic_name": "TestPost", "from_country_code": "CN"}
    post(client, "/settings", tok, **cfg)
    r = post(client, "/dev/simulate", tok, qty="3")
    oid = int(r.headers["location"].rsplit("/", 1)[1])
    assert "Needs approval" in client.get(f"/orders/{oid}").text
    assert "Approve and send" in client.get("/approvals").text
    assert client.get("/").text.count("1 order needs you") == 1
    post(client, f"/orders/{oid}/approve", tok)
    assert "With supplier" in client.get(f"/orders/{oid}").text


def test_settings_validation_rejects_garbage(client):
    tok = login(client)
    bad = {"min_margin_pct": "abc", "markup_pct": "60", "approval_threshold": "-5", "daily_order_cap": "x", "max_retries": "0",
           "retry_base_minutes": "2", "default_logistic_name": "", "from_country_code": "CHN"}
    post(client, "/settings", tok, **bad)
    page = client.get("/settings").text
    assert "must be a number" in page and "2 letters" in page


def test_product_form_validation(client):
    tok = login(client)
    post(client, "/products", tok, title="X", store_product_id="1", supplier_price="abc", selling_price="1")
    assert "must be a number" in client.get("/products").text


# ---- webhook + internal API ----------------------------------------------------------------
PAYLOAD = {"id": "5001", "number": "5001", "status": "processing", "name": "Asha", "email": "a@b.c", "total": "26.00",
           "country_code": "IN", "items": [{"store_product_id": "9001", "sku": "LAMP-1", "title": "Desk Lamp", "quantity": 1, "unit_price": "26.00"}]}


def test_webhook_signature_and_idempotency(client, web_cfg):
    tok = login(client)
    post(client, "/dev/seed", tok)
    body = json.dumps(PAYLOAD).encode()
    assert client.post("/webhooks/woocommerce", content=body).status_code == 401
    assert client.post("/webhooks/woocommerce", content=body, headers={"x-wc-webhook-signature": "bad"}).status_code == 401
    good = {"x-wc-webhook-signature": sign("mock-secret", body)}
    first = client.post("/webhooks/woocommerce", content=body, headers=good).json()
    second = client.post("/webhooks/woocommerce", content=body, headers=good).json()
    assert first["created"] is True and second["created"] is False and first["order_id"] == second["order_id"]
    db = client.app.state.database.SessionLocal()
    try:
        assert db.query(Order).filter_by(store_order_id="5001").count() == 1
        assert db.get(Order, first["order_id"]).status in {"submitted_to_supplier", "validated", "received", "pending_approval"}
    finally:
        db.close()


def test_webhook_ping_is_accepted_not_ingested(client):
    body = b"webhook_id=7"
    r = client.post("/webhooks/woocommerce", content=body, headers={"x-wc-webhook-signature": sign("mock-secret", body)})
    assert r.status_code == 200 and "ignored" in r.json()


def test_internal_jobs_need_key(client):
    assert client.post("/internal/jobs/process-pending").status_code == 401
    assert client.post("/internal/jobs/process-pending", headers={"x-internal-key": "wrong"}).status_code == 401
    h = {"x-internal-key": "i" * 32}
    for job in ["process-pending", "tracking-sync", "stock-price-sync", "intake-sweep", "reconcile"]:
        r = client.post(f"/internal/jobs/{job}", headers=h)
        assert r.status_code == 200, (job, r.text)
    r = client.post("/internal/alerts", headers=h, json={"workflow": "Tracking", "message": "boom"})
    assert r.json()["created"] is True
    assert "boom" in client.get("/alerts").text if login(client) else True


def test_health_is_public(client):
    assert client.get("/health").json() == {"ok": True, "version": VERSION}


def test_config_validation_blocks_unsafe_boot(tmp_path):
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(Config(app_env="test", database_url=f"sqlite:///{tmp_path}/x.db", secret_key="", internal_api_key="i" * 32, admin_password="x"))
    with pytest.raises(RuntimeError, match="DEV_TOOLS"):
        create_app(Config(app_env="production", database_url=f"sqlite:///{tmp_path}/y.db", secret_key="s" * 32, internal_api_key="i" * 32,
                          admin_password="x", enable_dev_tools=True))
