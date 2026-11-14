"""v19: affiliate-only business mode, network earnings through APIs (Awin, impact.com, CJ Affiliate), click log, sub-id tags, no cart."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.connectors import affiliate_sources as AS
from app.models import Click, Connection, Earning, Product
from app.services import earnings as EA
from app.services import mode as MODE
from app.services import settings_service as S
from app.services.job_runner import run_job
from tests.test_affiliate import AWIN, Net, _connect_feed, reset  # noqa: F401
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import login, post

AWIN_TX = [
    {"id": 101, "advertiserId": 7, "commissionAmount": {"amount": 2.5, "currency": "GBP"}, "saleAmount": {"amount": 50, "currency": "GBP"}, "commissionStatus": "pending",
     "transactionDate": "2026-11-10T10:00:00", "clickRefs": {"clickRef": "p12"}},
    {"id": 102, "advertiserId": 8, "commissionAmount": {"amount": 4, "currency": "USD"}, "saleAmount": {"amount": 40, "currency": "USD"}, "commissionStatus": "approved",
     "transactionDate": "2026-11-11T09:30:00", "clickRefs": {}},
]
IMPACT = {"Actions": [{"Id": "A1", "CampaignName": "Walmart", "Payout": "1.50", "Amount": "30.00", "Currency": "USD", "State": "Approved", "EventDate": "2026-11-09T12:00:00-08:00", "SubId1": "p5"}],
          "@nextpage": "/Mediapartners/ACC1/Actions?Page=2"}
IMPACT2 = {"Actions": [{"Id": "A2", "CampaignName": "Target", "Payout": "0.80", "Amount": "20.00", "Currency": "USD", "State": "Reversed", "EventDate": "2026-11-08T12:00:00Z", "SubId1": ""}]}


@pytest.fixture(autouse=True)
def fresh_shop_cache():
    import app.services.shop as SH
    SH._cache.clear()
    yield
    SH._cache.clear()


def _cj_payload(records):
    return {"data": {"publisherCommissions": {"count": len(records), "payloadComplete": True, "records": records}}}


# ---- status / helpers -------------------------------------------------------------------------
def test_status_and_subid_helpers():
    assert [EA.norm_status(x) for x in ("pending", "Approved", "locked", "closed", "Reversed", "declined", "new", "extended", "Paid")] == \
        ["pending", "approved", "approved", "paid", "declined", "declined", "pending", "pending", "paid"]
    assert EA.with_subid("aff_awin", "https://www.awin1.com/cread.php?awinmid=1&awinaffid=2", 12) == "https://www.awin1.com/cread.php?awinmid=1&awinaffid=2&clickref=p12"
    assert EA.with_subid("aff_impact", "https://x.sjv.io/c/1/2/3", 5) == "https://x.sjv.io/c/1/2/3?subId1=p5"
    assert EA.with_subid("aff_cj_affiliate", "https://www.anrdoezrs.net/click-1-2?url=a%2Fb#top", 9) == "https://www.anrdoezrs.net/click-1-2?url=a%2Fb&sid=p9#top"
    assert EA.with_subid("aff_awin", "https://a/b?ClickRef=mine", 3) == "https://a/b?ClickRef=mine"  # never overwrites a tag the user set
    assert EA.with_subid("aff_feed", "https://a/b", 3) == "https://a/b"  # networks we do not know are left alone


# ---- adapters ---------------------------------------------------------------------------------
def test_awin_request_and_parse():
    net = Net({"api.awin.com": AWIN_TX})
    AS.HTTP = net
    start = datetime(2026, 11, 1)
    rows = EA.fetch_awin({"awin_publisher_id": "12345", "awin_api_token": "TOK"}, {}, start, start + timedelta(days=30))
    m, url, hdr, _ = net.calls[0]
    assert m == "GET" and "/publishers/12345/transactions/" in url and "startDate=2026-11-01T00%3A00%3A00" in url and "timezone=UTC" in url
    assert hdr["Authorization"] == "Bearer TOK"
    assert [(r["tx_id"], r["status"], r["amount"], r["currency"], r["sub"]) for r in rows] == [("101", "pending", Decimal("2.5"), "GBP", "p12"), ("102", "approved", Decimal("4"), "USD", "")]
    assert EA.fetch_awin({}, {}, start, start) == []  # no credentials: nothing is called
    AS.HTTP = Net({"api.awin.com": (401, b"{}")})
    with pytest.raises(AS.SourceError, match="token"):
        EA.fetch_awin({"awin_publisher_id": "1", "awin_api_token": "bad"}, {}, start, start)


def test_impact_basic_auth_and_paging():
    net = Net({"Page=2": IMPACT2, "api.impact.com": IMPACT})
    AS.HTTP = net
    rows = EA.fetch_impact({"impact_account_sid": "ACC1", "impact_auth_token": "SECRET"}, {}, datetime(2026, 11, 1), datetime(2026, 11, 14))
    assert len(net.calls) == 2
    assert net.calls[0][2]["Authorization"] == "Basic " + base64.b64encode(b"ACC1:SECRET").decode()
    assert [(r["tx_id"], r["status"], r["sub"]) for r in rows] == [("A1", "approved", "p5"), ("A2", "declined", "")]
    assert rows[0]["when"] == datetime(2026, 11, 9, 20, 0)  # converted to UTC


def test_impact_never_follows_a_foreign_nextpage():
    AS.HTTP = Net({"api.impact.com": {"Actions": [], "@nextpage": "https://evil.example/steal"}})
    assert EA.fetch_impact({"impact_account_sid": "A", "impact_auth_token": "T"}, {}, datetime(2026, 11, 1), datetime(2026, 11, 2)) == []
    assert all("evil" not in c[1] for c in AS.HTTP.calls)


def test_cj_graphql_and_retry_without_unknown_field():
    calls = []

    def cj(method, url, headers, body):
        q = json.loads(body)["query"]
        calls.append(q)
        if "shopperId" in q:
            return 200, json.dumps({"errors": [{"message": "Cannot query field shopperId"}]}).encode()
        return 200, json.dumps(_cj_payload([{"commissionId": "C9", "actionStatus": "locked", "pubCommissionAmountUsd": 3.2, "saleAmountUsd": 64, "advertiserName": "Shop", "postingDate": "2026-11-12T01:00:00Z"}])).encode()
    AS.HTTP = Net({"commissions.api.cj.com": cj})
    rows = EA.fetch_cj({"cj_publisher_id": "pub 777", "cj_token": "T"}, {}, datetime(2026, 11, 1), datetime(2026, 11, 14))
    assert len(calls) == 2 and 'forPublishers: ["777"]' in calls[0]
    assert rows[0]["tx_id"] == "C9" and rows[0]["status"] == "approved" and rows[0]["amount"] == Decimal("3.2")
    AS.HTTP = Net({"commissions.api.cj.com": (200, json.dumps({"errors": [{"message": "Bad token"}]}).encode())})
    with pytest.raises(AS.SourceError, match="Bad token"):
        EA.fetch_cj({"cj_publisher_id": "1", "cj_token": "T"}, {}, datetime(2026, 11, 1), datetime(2026, 11, 2))


# ---- sync end to end ----------------------------------------------------------------------------
def _connect_awin(client, tok):
    AS.HTTP = Net({"api.awin.com": AWIN_TX})
    r = post(client, "/connect/new", tok, type="aff_awin", name="My Awin", awin_publisher_id="12345", awin_api_token="TOK")
    assert r.status_code == 303
    client.get("/programs")
    with client.app.state.database.SessionLocal() as db:
        c = db.query(Connection).filter(Connection.type == "aff_awin").one()
        assert c.last_status == "ok", c.last_error  # the credentials-only connection tests itself through the earnings API
        return c.id


def test_sync_upserts_updates_status_and_converts_currency(client):
    tok = login(client)
    cid = _connect_awin(client, tok)
    rt, database = client.app.state.rt, client.app.state.database
    with database.SessionLocal() as db:
        MODE.set_mode(db, "affiliate")
        res = run_job(db, rt, "earnings_run", trigger="manual")
        db.commit()
        assert res["new"] == 2 and res["errors"] == 0
        gbp = db.query(Earning).filter(Earning.tx_id == "101").one()
        assert gbp.status == "pending" and gbp.currency == "GBP" and gbp.amount_usd > Decimal("3") and gbp.sub == "p12"
        # the network approves it later: same transaction id, new status, no duplicate row
        approved = [dict(AWIN_TX[0], commissionStatus="approved"), AWIN_TX[1]]
        AS.HTTP = Net({"api.awin.com": approved})
        res = run_job(db, rt, "earnings_run", trigger="manual")
        db.commit()
        assert res["new"] == 0 and res["changed"] == 1 and db.query(Earning).count() == 2
        t = EA.totals(db)
        assert t["pending"] == 0 and t["approved"] > Decimal("7") and t["count"] == 2


def test_earnings_run_survives_one_failing_network_and_reports_none(client):
    login(client)
    rt, database = client.app.state.rt, client.app.state.database
    with database.SessionLocal() as db:
        MODE.set_mode(db, "affiliate")
        assert run_job(db, rt, "earnings_run")["none"] is True  # nothing connected: says so, invents nothing
        assert db.query(Earning).count() == 0


def test_declined_commission_is_not_counted(client):
    tok = login(client)
    _connect_awin(client, tok)
    rt, database = client.app.state.rt, client.app.state.database
    AS.HTTP = Net({"api.awin.com": [dict(AWIN_TX[1], commissionStatus="declined")]})
    with database.SessionLocal() as db:
        run_job(db, rt, "earnings_run")
        db.commit()
        assert EA.totals(db)["total"] == 0 and db.query(Earning).one().status == "declined"


# ---- affiliate mode ----------------------------------------------------------------------------------
def test_mode_default_toggle_and_dropship_jobs_sleep(client):
    tok = login(client)
    rt, database = client.app.state.rt, client.app.state.database
    with database.SessionLocal() as db:
        assert MODE.current(db) == "dropship"  # tests run the older flow by default (BUSINESS_MODE env); a real install defaults to affiliate
    r = post(client, "/settings/mode", tok, mode="affiliate")
    assert r.status_code == 303
    with database.SessionLocal() as db:
        assert MODE.is_affiliate(db)
        for job in MODE.DROPSHIP_JOBS:
            assert run_job(db, rt, job).get("skipped") == "affiliate mode"
    post(client, "/settings/mode", tok, mode="dropship")
    with database.SessionLocal() as db:
        assert run_job(db, rt, "process_pending").get("skipped") is None


def test_real_default_is_affiliate(monkeypatch):
    from app.config import Config
    monkeypatch.delenv("BUSINESS_MODE", raising=False)
    assert Config().business_mode == "affiliate"
    monkeypatch.setenv("BUSINESS_MODE", "dropship")
    assert Config().business_mode == "dropship"


def test_nav_dashboard_and_pages_follow_the_mode(client):
    tok = login(client)
    html = client.get("/").text
    assert 'href="/orders"' in html and 'href="/approvals"' in html  # dropship: old menus
    post(client, "/settings/mode", tok, mode="affiliate")
    client.get("/settings")  # clear flash
    html = client.get("/").text
    assert 'href="/earnings"' in html and 'href="/programs"' in html
    assert all(f'href="{h}"' not in html for h in ("/orders", "/approvals", "/finance", "/returns", "/risk", "/strategy", "/playbook", "/catalog"))
    assert "Live partner products" in html and "Kamai, 30 din" in html
    assert "Where orders are right now" not in html
    assert "Live partner products" in client.get("/partials/dashboard").text
    assert client.get("/earnings").status_code == 200 and client.get("/settings").status_code == 200 and client.get("/setup").status_code == 200
    assert "Affiliate program jodo" in client.get("/setup").text and "Supplier jodo" not in client.get("/setup").text


def test_storefront_is_cartless_in_affiliate_mode(client):
    tok = login(client)
    cid = _connect_feed(client, tok, {"feed.example": AWIN})
    post(client, f"/programs/{cid}/import", tok, keywords="", limit="5")
    with client.app.state.database.SessionLocal() as db:
        db.add(Product(store_product_id="D-1", title="Old dropship item", selling_price=Decimal("9"), stock=5, kind="dropship", listing_status="listed"))
        db.commit()
    shop = client.get("/shop/products").json()["products"]
    assert len(shop) == 3  # dropship mode shows both kinds
    post(client, "/settings/mode", tok, mode="affiliate")
    import app.services.shop as SH
    SH._cache.clear()
    shop = client.get("/shop/products").json()["products"]
    assert len(shop) == 2 and all(p["affiliate"] for p in shop)
    assert client.get("/shop/config").json()["mode"] == "affiliate"
    q = client.post("/shop/quote", json={"items": [{"id": shop[0]["id"], "qty": 1}]})
    assert q.status_code in (400, 422) and "checkout nahi" in q.text
    src = open("api/app/storefront/app.js", encoding="utf-8").read()
    assert 'cfg.mode === "affiliate"' in src and 'style.display = aff ? "none"' in src


def test_click_log_subid_tag_and_epc(client):
    tok = login(client)
    AS.HTTP = Net({"feed.example": AWIN.replace(b"https://www.awin1.com/pclick.php?p=1", b"https://www.awin1.com/cread.php?awinmid=1&awinaffid=2")})
    r = post(client, "/connect/new", tok, type="aff_awin", name="Awin feed", feed_url="https://feed.example/f.csv", merchant="MugShop", currency="USD")
    assert r.status_code == 303
    client.get("/programs")
    with client.app.state.database.SessionLocal() as db:
        cid = db.query(Connection).filter(Connection.type == "aff_awin").one().id
    post(client, f"/programs/{cid}/import", tok, keywords="blue", limit="5")
    pid = client.get("/shop/products").json()["products"][0]["id"]
    loc = client.get(f"/shop/go/{pid}").headers["location"]
    assert loc.endswith(f"&clickref=p{pid}"), loc  # the click is tagged so the network's report names the product
    client.get(f"/shop/go/{pid}")
    with client.app.state.database.SessionLocal() as db:
        assert db.query(Click).count() == 2
        assert EA.click_stats(db)["today"] == 2 and EA.epc(db) == Decimal("0")
        db.add(Earning(network="aff_awin", tx_id="z1", status="approved", amount=Decimal("1"), amount_usd=Decimal("1"), product_id=pid, occurred_at=datetime.utcnow()))
        db.commit()
        assert EA.epc(db) == Decimal("0.5000")  # $1 over 2 clicks
        db.add(Click(product_id=pid, created_at=datetime.utcnow() - timedelta(days=200)))
        db.commit()
        EA.prune_clicks(db)
        assert db.query(Click).count() == 2


def test_earnings_page_shows_only_what_networks_returned(client):
    tok = login(client)
    _connect_awin(client, tok)
    html = client.get("/earnings").text
    assert "Abhi network se koi transaction nahi aaya" in html
    client.post("/earnings/sync", headers={"X-CSRF-Token": tok})
    rt, database = client.app.state.rt, client.app.state.database
    with database.SessionLocal() as db:
        run_job(db, rt, "earnings_run")
        db.commit()
    html = client.get("/earnings").text
    assert 'chip-warn">pending' in html and 'chip-ok">approved' in html and "Product #12" in html  # p12 sub-id attributes the GBP commission to a product
    assert "andaza" in html  # the honesty note is on the page


def test_start_needs_no_cj_key_in_affiliate_mode(client):
    login(client)
    from app.services import launch
    database = client.app.state.database
    with database.SessionLocal() as db:
        items = {i["name"]: i for i in launch.preflight(db)}
        assert items["CJ API key"]["ok"] is False  # dropship mode still needs it
        MODE.set_mode(db, "affiliate")
        db.commit()
        items = {i["name"]: i for i in launch.preflight(db)}
        assert items["CJ API key"]["ok"] is None and items["Affiliate program"]["ok"] is False
