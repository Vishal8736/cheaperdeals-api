from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest

from app.adapters.base import StoreError
from app.db import utcnow
from app.models import Market, MarketPrice, Product, StrategyRec
from app.services import fx
from app.services import settings_service as S
from app.services import strategy as ST
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import login, post


@pytest.fixture(autouse=True)
def _no_network():
    fx.FETCHER = lambda: {}  # the exchange-rate feed is never called from tests
    yield
    fx.FETCHER = None


@pytest.fixture()
def world(db):
    ST.seed_markets(db)
    for c in ("USD", "INR", "GBP"):
        fx.set_rate(db, c, "1" if c == "USD" else {"INR": "84", "GBP": "0.8"}[c])  # confirmed rates
    us = db.query(Market).filter(Market.code == "US").one()
    us.shipping_usd, us.duty_pct, us.tax_pct, us.fee_pct = D("10"), D("0"), D("0"), D("3")
    p = Product(store_product_id="FAN", title="Table Fan", supplier_vid="V", supplier_price=D("24"), selling_price=D("40"), stock=9)
    db.add(p)
    db.commit()
    return p, us


def put(db, p, m, price, source="manual", age_days=0):
    row = ST.record_price(db, p, m, price, source=source)
    row.observed_at = utcnow() - timedelta(days=age_days)
    db.commit()


def test_the_fan_example_sells_at_1500_not_2000(db, world):
    p, us = world
    put(db, p, us, "2000")
    best = ST.plan(db, p)[0]
    assert best.market.code == "US" and best.status == "viable"
    assert best.price_usd == D("1500.00") and best.landed_usd == D("34.00")
    assert best.profit_usd == D("1500") - D("34") - D("45")  # minus 3% fee
    assert best.profit_usd > 0 and best.floor_usd < best.price_usd


def test_never_below_floor_and_needs_a_real_gap(db, world):
    p, us = world
    put(db, p, us, "40")  # market only slightly above our landed cost $34
    e = ST.plan(db, p)[0]
    assert e.status == "not_viable" and e.price_usd >= e.floor_usd and "Cannot beat the market" in e.reason
    put(db, p, us, "200")  # 25% off = 150, floor is far lower
    assert ST.plan(db, p)[0].price_usd == D("150.00")


def test_vat_duty_and_fees_raise_the_floor(db, world):
    p, us = world
    us.tax_pct, us.duty_pct = D("20"), D("10")
    put(db, p, us, "60")
    e = ST.plan(db, p)[0]
    # landed = (24+10)*1.10 = 37.40 ; denominator = 0.75/1.2 - 0.03 = 0.595
    assert e.landed_usd == D("37.40") and e.floor_usd == D("62.86")
    assert e.status == "not_viable"


def test_currency_conversion_and_rate_safety(db, world):
    p, us = world
    gb = db.query(Market).filter(Market.code == "GB").one()
    gb.shipping_usd, gb.tax_pct = D("10"), D("0")
    put(db, p, gb, "800")  # 800 GBP = $1000 at 0.8
    e = ST.plan(db, p)[0]
    assert e.ref_usd == D("1000.00") and e.price_usd == D("750.00") and e.price_local == D("600.00")
    fx.get(db, "GBP").source = "seed"  # an unconfirmed estimate must not drive a live price
    assert ST.plan(db, p)[0].status == "needs_rate"
    fx.get(db, "GBP").source = "manual"
    assert ST.plan(db, p)[0].status == "viable"


def test_stale_and_scraper_sanity(db, world):
    p, us = world
    put(db, p, us, "2000", age_days=30)
    assert ST.plan(db, p)[0].status == "stale"
    put(db, p, us, "5000", source="api")  # 147x our cost, from a scraper
    e = ST.plan(db, p)[0]
    assert e.status == "needs_check" and e.warnings
    put(db, p, us, "5000", source="manual")  # you typed it: trusted, but still warned
    e = ST.plan(db, p)[0]
    assert e.status == "viable" and e.warnings


def test_modes(db, world):
    p, us = world
    put(db, p, us, "1000")
    S.set_value(db, "strategy_mode", "max_profit")
    assert ST.plan(db, p)[0].price_usd == D("980.00")
    S.set_value(db, "strategy_mode", "aggressive")
    assert ST.plan(db, p)[0].price_usd == D("625.00")  # 37.5% under


def test_autopilot_off_only_suggests_and_on_applies(db, rt, world):
    p, us = world
    put(db, p, us, "2000")
    st = ST.run(db, rt)
    db.refresh(p)
    assert st["viable"] == 1 and st["applied"] == 0 and p.selling_price == D("40.00") and not p.strategy_managed
    S.set_value(db, "strategy_autopilot", "true")
    db.commit()
    st = ST.run(db, rt)
    db.refresh(p)
    assert st["applied"] == 1 and p.selling_price == D("1500.00") and p.strategy_managed and p.target_market_id == us.id
    assert db.query(StrategyRec).one().status == "applied"
    assert rt.store.get_price("FAN") == D("1500.00") if hasattr(rt.store, "get_price") else True
    assert ST.run(db, rt)["applied"] == 0  # same answer next time: no churn


def test_manual_override_is_never_touched(db, rt, world):
    p, us = world
    p.manual_override = True
    put(db, p, us, "2000")
    S.set_value(db, "strategy_autopilot", "true")
    db.commit()
    assert ST.run(db, rt)["applied"] == 0
    db.refresh(p)
    assert p.selling_price == D("40.00") and "Locked" in db.query(StrategyRec).one().reason


def test_store_failure_changes_nothing_and_run_continues(db, rt, world, monkeypatch):
    p, us = world
    put(db, p, us, "2000")
    S.set_value(db, "strategy_autopilot", "true")
    db.commit()
    from app.services import sync as Y
    monkeypatch.setattr(Y, "_push_price", lambda *a, **k: (_ for _ in ()).throw(StoreError("store down")))
    st = ST.run(db, rt)
    db.refresh(p)
    assert st["errors"] == 1 and p.selling_price == D("40.00") and not p.strategy_managed


def test_reverts_to_markup_when_gap_disappears(db, rt, world):
    p, us = world
    put(db, p, us, "2000")
    S.set_value(db, "strategy_autopilot", "true")
    db.commit()
    ST.run(db, rt)
    put(db, p, us, "45")  # market collapsed
    st = ST.run(db, rt)
    db.refresh(p)
    assert st["reverted"] == 1 and not p.strategy_managed and p.selling_price == D("38.40")  # 24 * 1.6


def test_stock_sync_keeps_strategy_price_and_converts_inr_costs(db, rt, world):
    from app.services import sync as Y
    p, us = world
    p.strategy_managed, p.selling_price, p.cost_currency = True, D("1500"), "INR"
    rt.supplier.set_product("V", 5, "2100.00")  # supplier quotes 2100 rupees
    db.commit()
    Y.stock_price_sync(db, rt)
    db.refresh(p)
    assert p.supplier_price == D("25.00") and p.selling_price == D("1500.00")


def test_missing_rate_and_bad_input_are_handled(db, world):
    p, us = world
    us.currency = "XYZ"
    put_row = MarketPrice(product_id=p.id, market_id=us.id, local_price=D("10"))
    db.add(put_row)
    db.commit()
    assert ST.plan(db, p)[0].status == "needs_rate"
    for bad in ("abc", "-5", "0", ""):
        with pytest.raises(ValueError):
            ST.record_price(db, p, us, bad)
    with pytest.raises(ValueError):
        ST.save_market(db, mid=None, code="??", name="x", currency="USD", shipping="1", duty="0", tax="0", fee="3", store_connection="", enabled=True)
    with pytest.raises(fx.FxError):
        fx.set_rate(db, "USD", "2")


def test_fx_refresh_never_overwrites_manual_and_survives_failure(db):
    ST.seed_markets(db)
    fx.set_rate(db, "INR", "90")
    fx.FETCHER = lambda: {"INR": 70, "EUR": 0.5, "GBP": "bad"}
    try:
        r = fx.refresh(db)
        assert r["ok"] and fx.rate(db, "INR") == D("90") and fx.rate(db, "EUR") == D("0.5") and fx.get(db, "EUR").source == "auto"
        def boom():
            raise RuntimeError("offline")
        fx.FETCHER = boom
        r = fx.refresh(db)
        assert not r["ok"] and fx.rate(db, "EUR") == D("0.5")
    finally:
        fx.FETCHER = None


def test_csv_import_and_settings_validation(db, world):
    p, us = world
    p.sku = "FAN-1"
    db.commit()
    ok, errs = ST.import_prices_csv(db, "sku,market,price,url\nFAN-1,US,2000,https://x\nNOPE,US,5,\nFAN-1,ZZ,5,\nFAN-1,US,abc,\n")
    assert ok == 1 and len(errs) == 3
    with pytest.raises(ValueError):
        ST.import_prices_csv(db, "a,b\n1,2\n")
    assert ST.save_settings(db, {"strategy_mode": "x", "strategy_undercut_pct": "200"})
    assert not ST.save_settings(db, {"strategy_mode": "undercut", "strategy_undercut_pct": "30", "strategy_min_profit_pct": "20", "strategy_min_gap_pct": "5",
                                     "strategy_max_age_days": "7", "strategy_sanity_ratio": "50", "strategy_autopilot": "on"})
    pr = ST.params(db)
    assert pr.autopilot and pr.undercut == D("30") and pr.max_age_days == 7


def test_pages_forms_and_api(client):
    tok = login(client)
    db = client.app.state.database.SessionLocal()
    db.add(Product(store_product_id="F2", title="Lamp", sku="LAMP", supplier_vid="V", supplier_price=D("10"), selling_price=D("20"), stock=3))
    db.commit()
    pid = db.query(Product).one().id
    assert client.get("/strategy").status_code == 200
    mid = db.query(Market).filter(Market.code == "US").one().id
    fx.set_rate(db, "USD", "1")
    db.commit()
    r = post(client, "/strategy/prices", tok, product=str(pid), market=str(mid), price="500")
    assert r.status_code == 303
    assert post(client, "/strategy/run", tok).status_code == 303
    page = client.get(f"/strategy/product/{pid}")
    assert page.status_code == 200 and "Good opportunity" in page.text
    assert client.get("/strategy").text.count("Lamp") >= 1
    assert post(client, f"/strategy/apply/{pid}", tok, market=str(mid)).status_code == 303
    db.expire_all()
    assert db.get(Product, pid).strategy_managed
    assert post(client, f"/strategy/revert/{pid}", tok).status_code == 303
    assert post(client, "/strategy/rates", tok, code="INR", per_usd="83").status_code == 303
    assert post(client, "/strategy/rates", tok, code="INR", per_usd="zero").status_code == 303
    assert post(client, "/strategy/markets", tok, code="SA", name="Saudi", currency="SAR", shipping="9", duty="5", tax="15", fee="3", enabled="on").status_code == 303
    assert db.query(Market).filter(Market.code == "SA").count() == 1
    assert post(client, "/strategy/settings", tok, strategy_mode="undercut", strategy_undercut_pct="20", strategy_min_profit_pct="20",
                strategy_min_gap_pct="5", strategy_max_age_days="14", strategy_sanity_ratio="30").status_code == 303
    assert client.get("/strategy/product/99999").status_code == 404
    # scraper API
    import os
    key = os.environ.get("INTERNAL_API_KEY") or client.app.state.cfg.internal_api_key
    h = {"X-Internal-Key": key}
    assert client.post("/internal/strategy/prices", json={"product": "LAMP", "market": "US", "price": "600"}, headers=h).status_code == 200
    assert client.post("/internal/strategy/prices", json={"product": "NOPE", "market": "US", "price": "600"}, headers=h).status_code == 422
    assert client.post("/internal/strategy/prices", json={"product": "LAMP", "market": "US", "price": "600"}).status_code in (401, 403)
    db.close()
