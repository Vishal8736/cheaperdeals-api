from __future__ import annotations

import io
import json
from decimal import Decimal

import pytest

from app.adapters.base import SupplierError
from app.adapters.mock_supplier import MockSupplier
from app.models import Order, Product
from app.services import analytics as AN
from app.services import orders as O
from app.services import outbound
from app.services import settings_service as S
from app.services import sync as Y
from app.services.pricing import sell_price
from tests.conftest import make_norm
from tests.test_agents_catalog import client, web_cfg, events  # noqa: F401 (fixtures)
from tests.test_web import login, post


def test_price_ending_never_rounds_down():
    assert sell_price(Decimal("9.10"), Decimal("60")) == Decimal("14.56")
    assert sell_price(Decimal("9.10"), Decimal("60"), "99") == Decimal("14.99")
    assert sell_price(Decimal("10"), Decimal("99"), "99") == Decimal("19.99")  # 19.90 -> 19.99
    assert sell_price(Decimal("10"), Decimal("100"), "99") == Decimal("20.99")  # 20.00 must not become 19.99
    assert sell_price(Decimal("10"), Decimal("50"), "x") == Decimal("15.00")


def test_failover_switches_to_backup_when_main_is_out_of_stock(db, rt, cfg, database, tmp_path):
    from app.connectors.manager import ConnectorManager
    rt.connectors = m = ConnectorManager(database, cfg.secret_key, tmp_path / "c")
    with database.session() as s:
        cid = m.save(s, conn=None, kind="supplier", type_key="mock_supplier", name="Backup co", cfg={}, sec={}).id
    m.supplier(cid, None).set_product("BV", 30, "11.00")
    rt.supplier.set_product("VID-1", 0, "10.00")  # main supplier: sold out
    p = Product(store_product_id="P1", title="Lamp", supplier_vid="VID-1", supplier_price=Decimal("10"), selling_price=Decimal("25"), stock=5,
                backup_connection_id=cid, backup_vid="BV")
    db.add(p)
    db.commit()
    st = Y.stock_price_sync(db, rt)
    db.refresh(p)
    assert st["failovers"] == 1 and p.supplier_connection_id == cid and p.supplier_vid == "BV" and p.stock == 30
    assert p.backup_vid == "VID-1" and p.backup_connection_id is None  # old main becomes the backup
    assert p.supplier_price == Decimal("11.00") and not p.auto_hidden  # still in stock and margin ok
    assert any("backup supplier" in m for _, _, m in events(database))


def test_no_failover_when_backup_also_empty_hides_product(db, rt, cfg, database, tmp_path):
    from app.connectors.manager import ConnectorManager
    rt.connectors = m = ConnectorManager(database, cfg.secret_key, tmp_path / "c")
    with database.session() as s:
        cid = m.save(s, conn=None, kind="supplier", type_key="mock_supplier", name="B", cfg={}, sec={}).id
    m.supplier(cid, None).set_product("BV", 0, "11.00")
    rt.supplier.set_product("VID-1", 0, "10.00")
    p = Product(store_product_id="P1", title="Lamp", supplier_vid="VID-1", supplier_price=Decimal("10"), selling_price=Decimal("25"), stock=5,
                backup_connection_id=cid, backup_vid="BV")
    db.add(p)
    db.commit()
    st = Y.stock_price_sync(db, rt)
    db.refresh(p)
    assert st["failovers"] == 0 and p.supplier_vid == "VID-1" and p.auto_hidden


def test_main_supplier_error_also_triggers_failover(db, rt, product):
    class Broken(MockSupplier):
        def get_product_info(self, vid):
            if vid == "VID-1":
                raise SupplierError("down", retryable=True)
            return super().get_product_info(vid)
    rt.supplier = Broken()
    rt.supplier.set_product("BV", 9, "10.50")
    product.backup_vid = "BV"
    db.commit()
    assert Y.stock_price_sync(db, rt)["failovers"] == 1


def test_outgoing_webhook_after_commit_only(db, rt, live, product):
    sent = []
    outbound.SENDER = lambda url, secret, body: sent.append((url, secret, json.loads(body)))
    try:
        S.set_value(db, "outgoing_webhook_url", "https://hooks.test/x")
        S.set_value(db, "outgoing_webhook_secret", "sek")
        db.commit()
        o, _, _ = O.intake(db, make_norm("7001"))
        assert sent == []  # nothing leaves before the commit
        db.commit()
        assert [e[2]["event"] for e in sent] == ["order.received"]
        O.process_order(db, rt, o.id)
        names = [e[2]["event"] for e in sent]
        assert "order.placed" in names and all(e[0] == "https://hooks.test/x" and e[1] == "sek" for e in sent)
        assert sent[-1][2]["data"]["number"] == "7001"
        sent.clear()
        S.set_value(db, "markup_pct", "1")
        outbound.emit(db, "x.test", {})
        db.rollback()
        assert sent == []  # rolled back work is never announced
    finally:
        outbound.SENDER = None


def test_signature_header_matches_body():
    import hashlib, hmac
    got = {}

    class R:
        status_code = 200

    import httpx
    real = httpx.post
    httpx.post = lambda url, content, headers, timeout: (got.update(headers=headers, body=content), R())[1]
    try:
        outbound._post("https://h.test", "sek", b'{"a":1}')
    finally:
        httpx.post = real
    assert got["headers"]["X-CheapeDeal-Signature"] == hmac.new(b"sek", b'{"a":1}', hashlib.sha256).hexdigest()


def test_analytics_numbers(db, rt, live, product, cfg):
    o, _, _ = O.intake(db, make_norm("8001", qty=2, unit="25.00"))
    db.commit()
    O.process_order(db, rt, o.id)
    r = AN.report(db, cfg, 30)
    assert r["orders"] == 1 and r["revenue"] == Decimal("50.00") and r["cost"] == Decimal("20.00") and r["profit"] == Decimal("30.00")
    assert r["margin"] == Decimal("60.0") and r["top"][0]["units"] == 2 and r["suppliers"][0][0] == "Built-in supplier"
    assert len(r["per_day"]) == 30 and sum(d["orders"] for d in r["per_day"]) == 1


def test_csv_roundtrip_and_pages(client):
    tok = login(client)
    csv_in = "title,sku,cost,price,stock,description\nBlue Mug,MUG-1,3.50,9.99,40,Nice\nRed Cup,CUP-1,2,6,10,\n,,,,,\n"
    r = client.post("/products/import-csv", data={"csrf_token": tok}, files={"file": ("p.csv", csv_in.encode())})
    assert r.status_code == 303
    exp = client.get("/products/export.csv")
    assert exp.status_code == 200 and "Blue Mug" in exp.text and "MUG-1" in exp.text and exp.headers["content-type"].startswith("text/csv")
    # same sku again updates, never duplicates
    client.post("/products/import-csv", data={"csrf_token": tok}, files={"file": ("p.csv", b"title,sku,price\nBlue Mug XL,MUG-1,12.50\n")})
    db = client.app.state.database.SessionLocal()
    assert db.query(Product).filter(Product.sku == "MUG-1").count() == 1
    p = db.query(Product).filter(Product.sku == "MUG-1").one()
    assert p.title == "Blue Mug XL" and p.selling_price == Decimal("12.50") and p.supplier_price == Decimal("3.50") and p.listing_status == "draft"
    db.close()
    bad = client.post("/products/import-csv", data={"csrf_token": tok}, files={"file": ("p.csv", b"name,price\nx,1\n")})
    assert bad.status_code == 303
    for url in ["/analytics", "/analytics?days=7", "/analytics?days=999", "/settings", "/connect/new?type=cj_dropshipping", "/connect/new?type=shopify"]:
        assert client.get(url).status_code == 200, url
    assert "Price ending" in client.get("/settings").text


def test_backup_fields_save_and_settings_validate(client):
    tok = login(client)
    db = client.app.state.database.SessionLocal()
    db.add(Product(store_product_id="b1", title="B", supplier_vid="V", selling_price=Decimal("5"), supplier_price=Decimal("2")))
    db.commit()
    pid = db.query(Product).one().id
    post(client, f"/products/{pid}", tok, supplier_vid="V", selling_price="5", backup_connection="0", backup_vid=" V2 ")
    db.expire_all()
    p = db.get(Product, pid)
    assert p.backup_vid == "V2" and p.backup_connection_id is None
    post(client, f"/products/{pid}", tok, supplier_vid="V", selling_price="5", backup_connection="0", backup_vid="")
    db.expire_all()
    assert db.get(Product, pid).backup_vid == ""
    form = {k: S.get(db, k) for k in S.DEFINITIONS}
    assert S.validate_and_save(db, {**form, "price_ending": "abc"})
    assert S.validate_and_save(db, {**form, "outgoing_webhook_url": "ftp://x"})
    assert not S.validate_and_save(db, {**form, "price_ending": "99", "outgoing_webhook_url": "https://ok.test"})
    assert S.get(db, "price_ending") == "99"
    db.close()


def test_cj_connection_through_the_ui_stores_secret_encrypted(client):
    tok = login(client)
    r = post(client, "/connect/new", tok, type="cj_dropshipping", name="My CJ", api_key="CJ1@api@TOP-SECRET-VALUE", from_country="CN", ship_to_country="US")
    assert r.status_code == 303
    db = client.app.state.database.SessionLocal()
    from app.models import Connection
    c = db.query(Connection).one()
    assert c.type == "cj_dropshipping" and "TOP-SECRET-VALUE" not in c.secrets_enc + c.config_json and "CN" in c.config_json
    db.close()

