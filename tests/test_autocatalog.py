"""The Catalog agent that fills the store by itself (before: nothing imported products unless a person picked them by hand)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.base import SupplierError
from app.models import Product
from app.services import autocatalog as AC
from app.services import settings_service as S
from app.services.job_runner import run_job
from app.services import assistant as AI
from tests.test_web import client, web_cfg  # noqa: F401


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(AC, "SLEEP", lambda s: None)
    monkeypatch.setattr(AC, "MockSupplier", type("NotTheMock", (), {}))  # the test supplier IS the mock; pretend it is a real one (one test below checks the guard)


def _go(db):
    S.set_value(db, "paused", "false")
    S.set_value(db, "auto_catalog_keywords", "e")
    db.commit()


def test_agent_imports_prices_and_lists_products_on_the_website(db, rt, monkeypatch):
    _go(db)
    S.set_value(db, "auto_catalog_batch", "5")
    db.commit()
    res = AC.run(db, rt)
    assert res["imported"] == 5 and res["listed"] + res["drafts"] == 5 and res["listed"] >= 1, res
    listed = db.query(Product).filter(Product.listing_status == "listed").all()
    assert listed and all(p.stock > 0 and Decimal(p.selling_price) > Decimal(p.supplier_price) and p.store_product_id.startswith("web-") for p in listed)
    from app.services import shop
    assert {p.id for p in listed} <= {p.id for p in shop.sellable(db).all()}  # really visible on the website
    again = AC.run(db, rt)  # next round brings DIFFERENT products (cursor moves on, duplicates are skipped)
    ids = [p.supplier_product_id for p in db.query(Product).all()]
    assert len(ids) == len(set(ids)) and again["imported"] >= 1


def test_agent_stops_at_target_and_respects_pause_and_off_switch(db, rt):
    _go(db)
    S.set_value(db, "auto_catalog_target", "2")
    S.set_value(db, "auto_catalog_batch", "8")
    db.commit()
    AC.run(db, rt)
    assert AC.counts(db)["listed"] >= 2
    assert AC.run(db, rt).get("full") is True
    S.set_value(db, "paused", "true")
    db.commit()
    assert AC.run(db, rt) == {"paused": True}
    S.set_value(db, "paused", "false")
    S.set_value(db, "auto_catalog", "false")
    db.commit()
    assert AC.run(db, rt) == {"off": True}


def test_product_without_supplier_stock_stays_draft_not_live(db, rt, supplier):
    _go(db)
    S.set_value(db, "auto_catalog_batch", "3")
    db.commit()
    orig = supplier.get_product_info

    def no_stock(vid):
        i = orig(vid)
        i.stock = 0
        return i
    supplier.get_product_info = no_stock
    res = AC.run(db, rt)
    assert res["listed"] == 0 and res["drafts"] == 3 and all("stock" in r for r in res["skipped_reasons"])
    assert db.query(Product).filter(Product.listing_status == "listed").count() == 0


def test_supplier_failure_is_reported_not_a_crash(db, rt, supplier):
    _go(db)

    def boom(*a, **k):
        raise SupplierError("CJ API key khali hai", retryable=False)
    supplier.list_catalog = boom
    res = AC.run(db, rt)
    assert "CJ API key" in res["error"]
    from app.models import AgentEvent
    assert db.query(AgentEvent).filter(AgentEvent.agent == "catalog", AgentEvent.level == "warn").count() >= 1


def test_job_runner_runs_catalog_job_and_records_an_agent_run(db, rt):
    _go(db)
    S.set_value(db, "auto_catalog_batch", "2")
    db.commit()
    res = run_job(db, rt, "catalog_run")
    assert res["imported"] == 2
    from app.models import AgentRun
    assert db.query(AgentRun).filter(AgentRun.agent == "catalog").count() == 1


def test_advisor_works_without_an_ai_model_using_real_numbers(db, rt):
    _go(db)
    res = AI.review(db)
    assert res["state"] == "done" and "Built-in jaanch" in res["text"] and "Abhi ek bhi product nahi" in res["text"]
    assert S.get(db, "ai_brief").startswith("Built-in jaanch")
    assert AI.review(db)["state"] == "same"  # nothing changed: no new line


def test_catalog_page_has_auto_panel_save_and_run_now(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(AC, "SLEEP", lambda s: None)
    from tests.test_web import login
    tok = login(client)
    page = client.get("/catalog").text
    assert "Auto catalog" in page and "Abhi chalao" in page
    r = client.post("/catalog/auto/save", data={"csrf_token": tok, "keywords": "e, a", "target": "9", "batch": "3", "on": "on"})
    assert r.status_code == 303
    assert 'value="e, a"' in client.get("/catalog").text and 'name="target" value="9"' in client.get("/catalog").text
    client.post("/engine/start", data={"csrf_token": tok, "next": "/"})
    r = client.post("/catalog/auto/run", data={"csrf_token": tok})
    assert r.status_code == 303
    with client.app.state.database.SessionLocal() as s:
        assert s.query(Product).count() >= 1


def test_busy_supplier_429_does_not_raise_alerts_and_does_not_hide_products(db, rt, supplier, product):
    """Screenshot: 'Could not read supplier data ... CJ HTTP 429' alerts for products that were fine."""
    from app.models import Alert
    from app.services import sync as Y

    def busy(vid):
        raise SupplierError("CJ HTTP 429", retryable=True, status_code=429)
    supplier.get_product_info = busy
    S.set_value(db, "paused", "false")
    db.commit()
    stats = Y.stock_price_sync(db, rt)
    assert stats["errors"] == 1 and stats["retry_later"] == 1
    assert db.query(Alert).filter(Alert.source == "stock_sync").count() == 0
    db.refresh(product)
    assert product.auto_hidden is False and product.stock == 50


def test_real_failures_still_alert(db, rt, supplier, product):
    from app.models import Alert
    from app.services import sync as Y

    def bad(vid):
        raise SupplierError("CJ has no variant X", retryable=False)
    supplier.get_product_info = bad
    S.set_value(db, "paused", "false")
    db.commit()
    Y.stock_price_sync(db, rt)
    assert db.query(Alert).filter(Alert.source == "stock_sync").count() == 1


def test_demo_supplier_never_fills_a_live_website(db, rt, monkeypatch):
    from app.adapters.mock_supplier import MockSupplier
    monkeypatch.setattr(AC, "MockSupplier", MockSupplier)
    _go(db)
    res = AC.run(db, rt)
    assert "CJ API key" in res["error"] and db.query(Product).count() == 0


def test_real_run_without_supplier_refuses_honestly_instead_of_faking(db, rt):
    from app.adapters.no_supplier import NoSupplier
    rt.supplier = NoSupplier()
    rt.connectors = None
    _go(db)
    res = AC.run(db, rt)
    assert "CJ API key" in res["error"] and db.query(Product).count() == 0
