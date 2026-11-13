from __future__ import annotations

import hashlib
import hmac
import json
import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.adapters.base import SupplierError
from app.adapters.mock_supplier import MockSupplier
from app.config import Config
from app.main import create_app
from app.models import AgentEvent, AgentRun, Connection, Order, Product, ProductListing
from app.services import agents as A
from app.services import catalog as C
from app.services import orders as O
from app.services.job_runner import run_job
from tests.conftest import db_url_for, make_norm
from tests.test_web import csrf, login, post


@pytest.fixture
def web_cfg(tmp_path):
    return Config(app_env="test", database_url=db_url_for(tmp_path, "a"), secret_key="s" * 32, internal_api_key="i" * 32,
                  admin_username="admin", admin_password="correct horse", timezone="UTC", enable_dev_tools=True,
                  supplier_backend="mock", store_backend="mock", connectors_dir=str(tmp_path / "connectors"), allow_connector_upload=True)


@pytest.fixture
def client(web_cfg):
    app = create_app(web_cfg)
    app.state.database.drop_all()
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.state.database.drop_all()
    app.state.database.engine.dispose()


def events(database):
    with database.session() as s:
        return [(e.agent, e.level, e.message) for e in s.query(AgentEvent).order_by(AgentEvent.id).all()]


# ---- live agents ---------------------------------------------------------------------------
def test_order_flow_writes_live_events(db, rt, live, product, database):
    order, created, _ = O.intake(db, make_norm("2001"))
    db.commit()
    assert created
    O.process_order(db, rt, order.id)
    ev = events(database)
    agents = [a for a, _, _ in ev]
    assert "intake" in agents and "router" in agents
    assert any("#2001" in m and "placed with the supplier" in m for _, _, m in ev)


def test_rolled_back_work_leaves_no_events(db, database):
    from app.services import settings_service as S
    S.set_value(db, "markup_pct", "99")  # real work in progress (open transaction)
    A.event(db, "router", "should vanish")
    db.rollback()
    assert events(database) == []


def test_track_records_runs_and_errors(db, database):
    with A.track(db, "tracking", "manual") as run:
        run.done(3, "Checked 3")
    with pytest.raises(RuntimeError):
        with A.track(db, "stock", "manual"):
            raise RuntimeError("boom")
    with A.track(db, "tracking", quiet_if_idle=True) as run:
        run.done(0, "idle")
    with database.session() as s:
        runs = s.query(AgentRun).order_by(AgentRun.id).all()
        assert [(r.agent, r.status, r.processed) for r in runs] == [("tracking", "ok", 3), ("stock", "error", 0)]
        assert "boom" in runs[1].error
    snap = A.snapshot(db)
    states = {c["key"]: c["state"] for c in snap["cards"]}
    assert states["tracking"] == "idle" and states["stock"] == "error" and states["catalog"] == "waiting"
    assert snap["total_runs"] == 2 and snap["total_processed"] == 3


def test_run_job_records_agent_run(db, rt, database):
    run_job(db, rt, "stock_price_sync", "manual")
    with database.session() as s:
        r = s.query(AgentRun).filter(AgentRun.agent == "stock").one()
        assert r.status == "ok" and r.trigger == "manual" and "Checked" in r.summary


# ---- catalog + listing ---------------------------------------------------------------------
def test_import_creates_drafts_once_and_lists_on_store(db, rt, store):
    items = C.search(db, rt, 0, "kitchen")
    assert len(items) == 2
    res = C.import_items(db, rt, 0, {i.supplier_product_id for i in items}, "kitchen", 1, markup_pct=Decimal("50"))
    db.commit()
    assert res["imported"] == 2
    p = db.query(Product).filter(Product.title == "Portable Blender Bottle").one()
    assert p.listing_status == "draft" and p.store_product_id.startswith("draft-") and p.selling_price == Decimal("11.10") and p.source == "import"
    again = C.import_items(db, rt, 0, {i.supplier_product_id for i in items}, "kitchen", 1)
    assert again["imported"] == 0 and again["skipped"] == 2
    ok, msg = C.list_on_store(db, rt, p, 0)
    db.commit()
    assert ok and p.listing_status == "listed" and p.store_product_id.startswith("mock-") and store.published[p.store_product_id].title == p.title
    assert C.list_on_store(db, rt, p, 0)[1].startswith("already listed")  # listing twice never duplicates
    assert db.query(ProductListing).filter(ProductListing.product_id == p.id).count() == 1


def test_listing_failure_is_recorded_not_raised(db, rt):
    p = Product(store_product_id="draft-1", title="Zero", supplier_vid="V", supplier_price=Decimal("1"), selling_price=Decimal("0"), listing_status="draft")
    db.add(p)
    db.commit()
    ok, msg = C.list_on_store(db, rt, p, 0)
    assert not ok and "selling price" in msg
    l = db.query(ProductListing).one()
    assert l.status == "error" and p.listing_status == "draft"


def test_orders_route_to_the_products_supplier_connection(db, rt, cfg, database, tmp_path, live):
    from app.connectors.manager import ConnectorManager
    m = ConnectorManager(database, cfg.secret_key, tmp_path / "c")
    rt.connectors = m
    with database.session() as s:
        cid = m.save(s, conn=None, kind="supplier", type_key="mock_supplier", name="Second", cfg={}, sec={}).id
    other = m.supplier(cid, None)
    other.set_product("OV", 10, "4.00")
    db.add(Product(store_product_id="P2", sku="S2", title="Other", supplier_vid="OV", supplier_price=Decimal("4"), selling_price=Decimal("20"), stock=10,
                   supplier_connection_id=cid))
    db.commit()
    o, _, _ = O.intake(db, make_norm("3001", pid="P2"))
    db.commit()
    assert O.process_order(db, rt, o.id)["outcome"] == "submitted"
    assert len(other.orders) == 1 and len(rt.supplier.orders) == 0  # went to the connection, not the built-in supplier


def test_mixed_supplier_order_is_held_for_you(db, rt, cfg, database, tmp_path, live, product):
    from app.connectors.manager import ConnectorManager
    from app.adapters.base import NormalizedItem
    rt.connectors = ConnectorManager(database, cfg.secret_key, tmp_path / "c")
    with database.session() as s:
        cid = rt.connectors.save(s, conn=None, kind="supplier", type_key="mock_supplier", name="S2", cfg={}, sec={}).id
    rt.connectors.supplier(cid, None).set_product("OV", 10, "4.00")
    db.add(Product(store_product_id="P2", sku="S2", title="Other", supplier_vid="OV", supplier_price=Decimal("4"), selling_price=Decimal("25"), stock=10,
                   supplier_connection_id=cid))
    db.commit()
    n = make_norm("3002")
    n.items.append(NormalizedItem("P2", "S2", "Other", 1, Decimal("25")))
    n.total = Decimal("50")
    o, _, _ = O.intake(db, n)
    db.commit()
    res = O.process_order(db, rt, o.id)
    assert db.get(Order, o.id).status == "failed_manual" and "different suppliers" in db.get(Order, o.id).last_error
    assert rt.supplier.calls.count("create_order") == 0


# ---- web -----------------------------------------------------------------------------------
def test_new_pages_render_and_need_login(client):
    for url in ["/agents", "/connect", "/catalog", "/partials/agents-live", "/connect/new?type=cj_dropshipping"]:
        assert client.get(url).status_code == 303, url
    login(client)
    for url in ["/agents", "/connect", "/catalog", "/catalog?go=1", "/products", "/products?view=draft", "/partials/agents-live", "/connect/new?type=cj_dropshipping", "/connect/new?type=custom_site"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert "Agents, live" in client.get("/").text and "Products in total" in client.get("/").text


def test_connect_flow_import_list_and_custom_site_webhook(client, web_cfg):
    tok = login(client)
    # supplier connection (demo), stored and tested
    r = post(client, "/connect/new", tok, type="mock_supplier", name="Demo supplier")
    assert r.status_code == 303
    page = client.get("/connect").text
    assert "Demo supplier" in page and "Connected" in page
    sup_id = client.app.state.database.SessionLocal().query(Connection).filter(Connection.kind == "supplier").one().id
    # browse + import
    r = client.get(f"/catalog?source={sup_id}&q=yoga&go=1")
    assert "Yoga Mat" in r.text
    pid = re.search(r'name="pick" value="([^"]+)"', r.text).group(1)
    r = client.post("/catalog/import", data={"csrf_token": tok, "source": sup_id, "q": "yoga", "pg": 1, "pick": pid, "markup": "100"})
    assert r.status_code == 303 and r.headers["location"] == "/products?view=draft"
    assert "Yoga Mat" in client.get("/products?view=draft").text
    # custom website store
    r = post(client, "/connect/new", tok, type="custom_site", name="My site", base_url="https://my.invalid", webhook_secret="whsec", api_key="k",
             publish_path="/api/products", auth_header_name="X-Key", auth_header_value="{api_key}")
    assert r.status_code == 303
    db = client.app.state.database.SessionLocal()
    site = db.query(Connection).filter(Connection.type == "custom_site").one()
    assert site.last_status == "error"  # my.invalid is unreachable: saved but test failed, never crashes
    assert "k" != site.secrets_enc and "whsec" not in site.secrets_enc

    # swap the site adapter for a fake that accepts products, then list the draft on it
    from app.connectors.custom_site import CustomSiteStore
    import httpx
    site_adapter = CustomSiteStore({"base_url": "https://my.invalid"}, "k", "whsec",
                                   client=httpx.Client(transport=httpx.MockTransport(lambda rq: httpx.Response(200, json={"id": "site-9", "url": "https://my.invalid/p/9"}))))
    client.app.state.connectors._cache[site.id] = ((site.type, site.config_json, site.secrets_enc), site_adapter)
    prod = db.query(Product).filter(Product.title == "Yoga Mat Anti-Slip").one()
    r = client.post("/products/list-on-store", data={"csrf_token": tok, "pid": prod.id, "store": site.id})
    assert r.status_code == 303
    db.expire_all()
    l = db.query(ProductListing).filter(ProductListing.product_id == prod.id).one()
    assert l.status == "listed" and l.remote_id == "site-9" and db.get(Product, prod.id).listing_status == "listed"
    assert "My site" in client.get("/products").text
    # the website sends a paid order for that product -> becomes an order, matched via the listing id
    raw = json.dumps({"id": "W1", "status": "paid", "total": "40", "currency": "USD", "customer": {"name": "Ravi", "email": "r@x.com"},
                      "shipping": {"address1": "x", "city": "Pune", "zip": "4", "country_code": "IN"},
                      "items": [{"product_id": "site-9", "title": "Yoga", "quantity": 1, "unit_price": "40"}]}).encode()
    sig = hmac.new(b"whsec", raw, hashlib.sha256).hexdigest()
    assert client.post(f"/webhooks/store/{site.id}", content=raw, headers={"x-signature": "bad"}).status_code == 401
    assert client.post("/webhooks/store/9999", content=raw, headers={"x-signature": sig}).status_code == 404
    r = client.post(f"/webhooks/store/{site.id}", content=raw, headers={"x-signature": sig})
    assert r.status_code == 200 and r.json()["created"] is True
    db.expire_all()
    o = db.query(Order).filter(Order.store_order_id == "W1").one()
    assert o.store_connection_id == site.id and o.items[0].product_id == prod.id
    assert client.post(f"/webhooks/store/{site.id}", content=raw, headers={"x-signature": sig}).json()["created"] is False  # idempotent
    # live feed shows it
    assert "New order #W1" in client.get("/partials/agents-live").text
    db.close()


def test_connection_can_be_switched_off_and_delete_is_protected(client):
    tok = login(client)
    post(client, "/connect/new", tok, type="mock_supplier", name="Demo")
    db = client.app.state.database.SessionLocal()
    c = db.query(Connection).one()
    assert post(client, f"/connect/{c.id}/toggle", tok).status_code == 303
    db.expire_all()
    assert db.get(Connection, c.id).enabled is False
    db.add(Product(store_product_id="x", title="X", supplier_connection_id=c.id))
    db.commit()
    post(client, f"/connect/{c.id}/delete", tok)
    db.expire_all()
    assert db.get(Connection, c.id) is not None  # in use: refuses to delete
    db.close()


def test_feed_requires_token(client):
    tok = login(client)
    db = client.app.state.database.SessionLocal()
    from app.services import settings_service as S
    t = S.get(db, "feed_token")
    db.add(Product(store_product_id="f1", title="Feed me", selling_price=Decimal("5"), stock=3, listing_status="listed"))
    db.add(Product(store_product_id="f2", title="Draft one", selling_price=Decimal("5"), stock=3, listing_status="draft"))
    db.commit()
    assert client.get("/feed/products.json").status_code == 401
    assert client.get("/feed/products.json?token=nope").status_code == 401
    j = client.get(f"/feed/products.json?token={t}").json()
    assert j["count"] == 1 and j["products"][0]["title"] == "Feed me"
    db.close()


def test_connector_upload_validates(client, web_cfg):
    tok = login(client)
    good = b'from app.connectors.sdk import StoreConnector\nclass Connector(StoreConnector):\n    key="upl"\n    label="Uploaded"\n'
    r = client.post("/connect/upload", data={"csrf_token": tok}, files={"file": ("upl.py", good)})
    assert r.status_code == 303 and "Uploaded" in client.get("/connect").text
    for name, body in [("../evil.py", good), ("bad.txt", good), ("syn.py", b"def (:")]:
        client.post("/connect/upload", data={"csrf_token": tok}, files={"file": (name, body)})
    import os
    from pathlib import Path
    files = sorted(f for f in os.listdir(web_cfg.connectors_dir) if f.endswith(".py"))
    assert files == ["evil.py", "upl.py"]  # a path in the file name is stripped to its basename; .txt and invalid Python are refused
    assert not (Path(web_cfg.connectors_dir).parent / "evil.py").exists()  # nothing escaped the folder


def test_internal_job_endpoints_still_work_and_record_runs(client):
    h = {"X-Internal-Key": "i" * 32}
    assert client.post("/internal/jobs/stock-price-sync", headers=h).status_code == 200
    assert client.post("/internal/jobs/tracking-sync", headers=h).status_code == 200
    assert client.post("/internal/jobs/process-pending", headers=h).status_code == 200
    login(client)
    assert "Stock and Price" in client.get("/agents").text


def test_template_download(client):
    login(client)
    assert client.get("/connect/template/supplier.py").status_code == 404  # CJ is the only real supplier
    r = client.get("/connect/template/store.py")
    assert r.status_code == 200 and "class Connector(" in r.text
    assert client.get("/connect/template/nope.py").status_code == 404
