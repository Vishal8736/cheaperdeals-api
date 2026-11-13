from __future__ import annotations

from decimal import Decimal as D

import pytest

from app import security
from app.adapters.mock_store import MockStore
from app.models import AppUser, Market, MarketPrice, Order, Product, ProductListing, ReturnCase, TrackingEvent, SupplierOrder
from app.services import fx, mailer, ops as OPS, orders as O, pricefeed as PF, finance as FIN, risk as RISK
from app.services import settings_service as S
from app.services import strategy as ST
from app.services import sync as Y
from tests.conftest import make_norm
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import csrf, login, post


@pytest.fixture(autouse=True)
def _hooks():
    fx.FETCHER = lambda: {}
    yield
    fx.FETCHER = None
    PF.HTTP = None
    PF._token_cache.clear()
    mailer.SENDER = None
    OPS.PING = None


def ingest(db, n="1", **kw):
    o, *_ = O.intake(db, make_norm(n, **kw))
    db.commit()
    return o


# ---- risk ----------------------------------------------------------------------------------
def test_blocked_country_holds_order_for_approval(db, rt, live, product):
    S.set_value(db, "risk_blocked_countries", "IN")
    db.commit()
    o = ingest(db, "R1")
    O.process_order(db, rt, o.id)
    db.refresh(o)
    assert o.status == "pending_approval" and o.risk_score >= 60 and "blocked shipping" in o.risk_reasons.lower()


def test_normal_order_is_not_held(db, rt, live, product):
    o = ingest(db, "R2")
    O.process_order(db, rt, o.id)
    db.refresh(o)
    assert o.status != "pending_approval" and o.risk_score < 60


# ---- finance + returns ---------------------------------------------------------------------
def test_pnl_uses_frozen_cost_and_refunds(db, rt, live, product):
    o = ingest(db, "F1", qty=2, unit="25.00")
    assert o.items[0].unit_cost == D("10.00")
    product.supplier_price = D("99")  # supplier price changes later: the old order must keep its own cost
    db.commit()
    FIN.set_actual_ship_cost(db, o, "5")
    r = FIN.open_return(db, o, "damaged")
    FIN.move_return(db, rt, r, "approved")
    FIN.move_return(db, rt, r, "received")
    FIN.move_return(db, rt, r, "refunded", refund="20", supplier_refund="8")
    db.commit()
    t = FIN.pnl(db)["total"]
    assert t["revenue"] == D("50.00") and t["cost"] == D("20.00")
    assert t["refund"] == D("20.00")


def test_refund_needs_an_amount(db, rt, product):
    o = ingest(db, "F2")
    r = FIN.open_return(db, o, "x")
    FIN.move_return(db, rt, r, "approved")
    with pytest.raises(Exception):
        FIN.move_return(db, rt, r, "refunded", refund="0")


# ---- price feed ----------------------------------------------------------------------------
def _ebay(prices, currency="USD"):
    def http(method, url, headers, body):
        if "oauth2/token" in url:
            return {"access_token": "T", "expires_in": 7200}
        return {"itemSummaries": [{"price": {"value": str(v), "currency": currency}} for v in prices]}
    return http


def test_robust_median_drops_junk_and_needs_five():
    assert PF.robust_median([D(x) for x in (100, 110, 120, 125, 130, 5, 900)]) == D("120.00")
    assert PF.robust_median([D(x) for x in (100, 110)]) is None


def test_price_feed_records_api_price(db, rt):
    ST.seed_markets(db)
    p = Product(store_product_id="X", title="Fan", supplier_vid="V", supplier_price=D("24"), selling_price=D("40"), stock=5, search_query="table fan")
    db.add(p)
    PF.SECRET_KEY = "x" * 32
    PF.save_credentials(db, "id", "secret")
    S.set_value(db, "pricefeed_enabled", "true")
    db.commit()
    PF.HTTP = _ebay([1900, 2000, 2100, 2000, 1950])
    res = PF.run(db, rt)
    mp = db.query(MarketPrice).join(Market, Market.id == MarketPrice.market_id).filter(Market.code == "US").one()
    assert res["updated"] >= 1 and mp.local_price == D("2000.00") and mp.source == "api"


def test_price_feed_off_without_keys(db, rt):
    PF.SECRET_KEY = "x" * 32
    assert PF.run(db, rt)["off"] is True


# ---- multi-market --------------------------------------------------------------------------
class Conns:
    def __init__(self, stores):
        self.stores = stores

    def store(self, cid, default):
        return self.stores.get(cid, default)

    def supplier(self, cid, default):
        return default


def test_multi_market_lists_each_store_at_its_own_price(db, rt):
    ST.seed_markets(db)
    for c, v in (("USD", "1"), ("GBP", "0.8")):
        fx.set_rate(db, c, v)
    s1, s2 = MockStore(), MockStore()
    rt.connectors = Conns({11: s1, 22: s2})
    from app.models import Connection
    for cid in (11, 22):
        db.add(Connection(id=cid, kind="store", type="mock_store", name=f"Store{cid}"))
    us = db.query(Market).filter(Market.code == "US").one()
    gb = db.query(Market).filter(Market.code == "GB").one()
    us.store_connection_id, gb.store_connection_id = 11, 22
    us.shipping_usd = gb.shipping_usd = D("10")
    us.duty_pct = gb.duty_pct = D("0")
    us.tax_pct, gb.tax_pct = D("0"), D("0")
    p = Product(store_product_id="FAN", title="Fan", supplier_vid="V", supplier_price=D("24"), selling_price=D("40"), stock=5)
    db.add(p)
    db.commit()
    ST.record_price(db, p, us, "2000")
    ST.record_price(db, p, gb, "1200")
    S.set_value(db, "strategy_multi_market", "true")
    S.set_value(db, "strategy_autopilot", "true")
    db.commit()
    ST.run(db, rt)
    lst = {l.connection_id: l for l in db.query(ProductListing).filter(ProductListing.product_id == p.id)}
    assert set(lst) == {11, 22}
    prices = sorted(next(iter(s.published.values())).price for s in (s1, s2))
    assert prices[1] == D("1500.00") and prices[0] < prices[1]
    own = [l for l in lst.values() if l.price_usd is not None]
    assert len(own) == 1  # the best market's store follows the product price; the other one has its own


def test_product_price_change_skips_stores_with_own_price(db, rt):
    s1 = MockStore()
    rt.connectors = Conns({5: s1})
    p = Product(store_product_id="P", title="T", supplier_vid="V", supplier_price=D("5"), selling_price=D("10"), stock=1, listing_status="listed")
    db.add(p)
    db.flush()
    db.add(ProductListing(product_id=p.id, connection_id=5, remote_id="r1", status="listed", price_usd=D("77")))
    db.commit()
    Y._push_price(rt, p, D("12"))
    assert "r1" not in s1.prices


# ---- parallel sync -------------------------------------------------------------------------
def test_stock_sync_many_products_in_parallel(db, rt, supplier, live):
    for i in range(8):
        supplier.set_product(f"V{i}", stock=7, price="10.00")
        db.add(Product(store_product_id=f"S{i}", title=f"P{i}", supplier_vid=f"V{i}", supplier_price=D("10"), selling_price=D("25"), stock=1))
    db.commit()
    res = Y.stock_price_sync(db, rt)
    assert res["checked"] == 8 and res["errors"] == 0
    assert all(p.stock == 7 for p in db.query(Product).filter(Product.store_product_id.like("S%")))


# ---- customer email ------------------------------------------------------------------------
def test_customer_email_sent_once_when_enabled(db, rt, live, product):
    sent = []
    mailer.SENDER = sent.append
    S.set_value(db, "customer_emails", "true")
    o = ingest(db, "M1")
    ev = TrackingEvent(order_id=o.id, status="shipped", tracking_number="TRK1", carrier="DHL")
    db.add(ev)
    db.commit()
    Y._notify_store(db, rt, o, ev)
    Y._notify_store(db, rt, o, ev)
    assert len(sent) == 1 and sent[0]["To"] == "asha@example.com" and "TRK1" in sent[0].get_content()


def test_no_customer_email_when_off(db, rt, live, product):
    sent = []
    mailer.SENDER = sent.append
    o = ingest(db, "M2")
    ev = TrackingEvent(order_id=o.id, status="shipped", tracking_number="T", carrier="")
    db.add(ev)
    db.commit()
    Y._notify_store(db, rt, o, ev)
    assert sent == []


# ---- heartbeat -----------------------------------------------------------------------------
def test_heartbeat_pings_only_with_url(db, cfg):
    hits = []
    OPS.PING = hits.append
    assert OPS.heartbeat(db, cfg) == {"pinged": False}
    S.set_value(db, "heartbeat_url", "https://hc-ping.com/abc")
    assert OPS.heartbeat(db, cfg)["pinged"] is True and hits == ["https://hc-ping.com/abc"]


# ---- 2FA + roles ---------------------------------------------------------------------------
def test_totp_rfc6238_vector():
    assert security.totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59) == "287082"
    assert not security.totp_ok("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", "abc")


def test_2fa_login_flow(client):
    tok = login(client)
    r = post(client, "/users/2fa/start", tok)
    assert r.status_code == 303
    from app.services import secretbox_settings as SB
    db = client.app.state.database.SessionLocal()
    sec = SB.pending_secret(db, client.app.state.cfg)
    db.close()
    r = post(client, "/users/2fa/confirm", tok, code=security.totp_code(sec))
    client.post("/logout", data={"csrf_token": tok})
    bad = client.post("/login", data={"username": "admin", "password": "correct horse"})
    assert bad.status_code == 401
    wrong = client.post("/login", data={"username": "admin", "password": "correct horse", "code": "000000"})
    assert wrong.status_code == 401
    ok = client.post("/login", data={"username": "admin", "password": "correct horse", "code": security.totp_code(sec)})
    assert ok.status_code == 303


def test_operator_and_viewer_limits(client):
    tok = login(client)
    for name, role in (("oper", "operator"), ("view", "viewer")):
        assert post(client, "/users", tok, username=name, password="longpassword1", role=role).status_code == 303
    client.post("/logout", data={"csrf_token": tok})
    # operator: daily pages yes, admin pages no
    assert client.post("/login", data={"username": "oper", "password": "longpassword1"}).status_code == 303
    assert client.get("/orders").status_code == 200
    assert client.get("/finance").status_code == 200
    assert client.get("/settings").status_code == 403 and client.get("/users").status_code == 403 and client.get("/system").status_code == 403
    tok2 = csrf(client)
    client.post("/logout", data={"csrf_token": tok2})
    # viewer: look only
    assert client.post("/login", data={"username": "view", "password": "longpassword1"}).status_code == 303
    assert client.get("/orders").status_code == 200
    tok3 = csrf(client)
    assert post(client, "/risk", tok3, risk_hold_score="10").status_code == 403


def test_disabled_user_cannot_login(client):
    tok = login(client)
    post(client, "/users", tok, username="gone", password="longpassword1", role="viewer")
    db = client.app.state.database.SessionLocal()
    uid = db.query(AppUser).filter(AppUser.username == "gone").one().id
    db.close()
    post(client, f"/users/{uid}/toggle", tok)
    client.post("/logout", data={"csrf_token": tok})
    assert client.post("/login", data={"username": "gone", "password": "longpassword1"}).status_code == 401


# ---- pages ---------------------------------------------------------------------------------
def test_new_pages_render(client):
    login(client)
    for url in ("/guide", "/finance", "/returns", "/risk", "/users", "/system", "/strategy"):
        r = client.get(url)
        assert r.status_code == 200, url
    assert "Cheape Deal ka poora Guide" in client.get("/guide").text
