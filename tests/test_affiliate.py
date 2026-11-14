"""v18: affiliate feeds, Amazon/eBay live search, Printful/Printify, the 30-program catalog, 'Buy at' links with click counting, agents leaving affiliate products alone."""
from __future__ import annotations

import gzip
import json
from decimal import Decimal

import pytest

from app.adapters.base import SupplierError, SupplierLine, SupplierOrderRequest
from app.connectors import affiliate_sources as AS
from app.connectors import feedparse as F
from app.connectors import pod
from app.connectors.programs import PROGRAMS, by_key, ranked
from app.models import Connection, Product
from app.services import affiliate as AF
from app.services import fx
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import login, post

AWIN = (b"aw_product_id,product_name,search_price,aw_deep_link,aw_image_url,description,merchant_category,currency\n"
        b"1,Blue Mug,12.50,https://www.awin1.com/pclick.php?p=1,https://i.example/1.jpg,<b>Nice</b> mug,Home,USD\n"
        b"2,Red Mug,9.00,https://www.awin1.com/pclick.php?p=2,https://i.example/2.jpg,Red mug,Home,USD\n"
        b"3,,5,https://x/y,,,,USD\n")


class Net:
    """Fake internet: routes by URL substring, records every call."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        for key, resp in self.routes.items():
            if key in url:
                r = resp(method, url, headers, body) if callable(resp) else resp
                return r if isinstance(r, tuple) else (200, r if isinstance(r, bytes) else json.dumps(r).encode())
        return 404, b"{}"


@pytest.fixture(autouse=True)
def reset():
    AS._feed_cache.clear()
    yield
    AS.HTTP = None
    AS._feed_cache.clear()


# ---- feed parsing ---------------------------------------------------------------------------
def test_feed_formats():
    offers = F.parse(AWIN)
    assert [o.offer_id for o in offers] == ["1", "2"]  # the row without a title is skipped
    assert offers[0].price == Decimal("12.50") and offers[0].description.strip().startswith("Nice") and "<b>" not in offers[0].description
    xml = b'<rss xmlns:g="http://base.google.com/ns/1.0"><channel><item><g:id>7</g:id><g:title>Shoe</g:title><g:price>1,299.00 INR</g:price><g:link>https://x.com/s</g:link></item></channel></rss>'
    o = F.parse(xml)[0]
    assert (o.price, o.currency) == (Decimal("1299.00"), "INR")
    yml = b'<yml_catalog><shop><offers><offer id="5"><name>Cap</name><price>7</price><currencyId>USD</currencyId><url>https://c/cap</url></offer></offers></shop></yml_catalog>'
    assert F.parse(yml)[0].offer_id == "5"
    js = json.dumps({"data": {"products": [{"id": 9, "name": "Lamp", "price": "1.299,50 EUR", "url": "https://l/1", "image": {"url": "https://l/i.jpg"}}]}}).encode()
    o = F.parse(js)[0]
    assert o.price == Decimal("1299.50") and o.currency == "EUR"
    tsv = b"id\tname\tprice\tlink\n1\tPen\t2.5\thttps://p/1\n"
    assert F.parse(tsv)[0].title == "Pen"
    assert F.parse(gzip.compress(AWIN))[1].title == "Red Mug"


def test_feed_rejects_bad_input_safely():
    with pytest.raises(F.FeedParseError):
        F.parse(b'<!DOCTYPE x [<!ENTITY a "b">]><r><item><title>t</title></item></r>')
    with pytest.raises(F.FeedParseError):
        F.parse(b"name,price\nA,5\n")  # no web link anywhere
    with pytest.raises(F.FeedParseError):
        F.parse(b"")
    assert F.parse_price("free") is None and F.parse_price("0") is None and F.parse_price("Rs. 1,299") == Decimal("1299")


# ---- sources ---------------------------------------------------------------------------------
def test_feed_source_search_deeplink_and_private_urls_blocked():
    AS.HTTP = Net({"feed.example": AWIN})
    prog = {"key": "aff_feed", "label": "Universal"}
    s = AS.FeedSource({"deeplink_prefix": "https://track.example/?id=9&url={url_enc}", "commission_pct": "8", "currency": "USD"}, {"feed_url": "https://feed.example/f.csv"}, prog)
    assert "2 product" in s.test_connection() or "2 " in s.test_connection()
    got = s.search("red mug")
    assert [o.offer_id for o in got] == ["2"]
    assert got[0].url.startswith("https://track.example/?id=9&url=https%3A%2F%2Fwww.awin1.com") and got[0].commission_pct == Decimal("8")
    bad = AS.FeedSource({}, {"feed_url": "http://127.0.0.1/feed.csv"}, prog)
    with pytest.raises(AS.SourceError):
        bad.test_connection()
    with pytest.raises(AS.SourceError):
        AS.FeedSource({}, {"feed_url": "file:///etc/passwd"}, prog).load()


def test_amazon_creators_login_and_search():
    seen = {}

    def search(m, u, h, b):
        seen["h"], seen["b"] = h, json.loads(b)
        return {"searchResult": {"items": [
            {"asin": "B0A", "detailPageURL": "https://www.amazon.com/dp/B0A?tag=mytag-20", "itemInfo": {"title": {"displayValue": "Kettle"}},
             "images": {"primary": {"large": {"url": "https://m/k.jpg"}}}, "offersV2": {"listings": [{"price": {"money": {"amount": 29.99, "currency": "USD"}}}]}},
            {"asin": "B0B", "detailPageURL": "https://www.amazon.com/dp/B0B", "itemInfo": {"title": {"displayValue": "No price"}}}]}}
    AS.HTTP = Net({"api.amazon.com/auth": {"access_token": "TOK", "expires_in": 3600}, "creatorsapi.amazon/catalog/v1/searchItems": search})
    a = AS.AmazonCreatorsSource({"partner_tag": "mytag-20", "marketplace": "www.amazon.com", "credential_version": "3.1"}, {"client_id": "i", "client_secret": "s"}, {"key": "aff_amazon", "label": "Amazon Associates (Creators API)"})
    got = a.search("kettle")
    assert [o.title for o in got] == ["Kettle"]  # the item without a price is never invented
    assert seen["h"]["Authorization"] == "Bearer TOK" and seen["h"]["x-marketplace"] == "www.amazon.com" and seen["b"]["partnerTag"] == "mytag-20"
    assert "Amazon Creators API" in a.test_connection()
    with pytest.raises(AS.SourceError):
        AS.AmazonCreatorsSource({}, {"client_id": "i", "client_secret": "s"}, {"key": "k", "label": "Amazon"}).test_connection()


def test_ebay_only_keeps_tracked_links():
    seen = {}

    def srch(m, u, h, b):
        seen.update(h)
        return {"itemSummaries": [{"itemId": "1", "title": "Watch", "price": {"value": "49.00", "currency": "USD"}, "itemAffiliateWebUrl": "https://rover.ebay.com/x", "image": {"imageUrl": "https://i/w.jpg"}},
                                  {"itemId": "2", "title": "Plain", "price": {"value": "5", "currency": "USD"}, "itemWebUrl": "https://www.ebay.com/itm/2"}]}
    AS.HTTP = Net({"identity/v1/oauth2/token": {"access_token": "T", "expires_in": 7200}, "buy/browse/v1/item_summary/search": srch})
    e = AS.EbaySource({"campaign_id": "5338", "marketplace": "EBAY_US"}, {"client_id": "a", "client_secret": "b"}, {"key": "aff_ebay", "label": "eBay"})
    got = e.search("watch")
    assert [o.offer_id for o in got] == ["1"]
    assert "affiliateCampaignId=5338" in seen["X-EBAY-C-ENDUSERCTX"]


# ---- POD suppliers ---------------------------------------------------------------------------
def _req(vid):
    return SupplierOrderRequest(order_number="W1001", customer_name="Asha Rao", email="a@x.com", phone="999", address1="1 Main", address2="", city="Pune", province="MH",
                                zip_code="411001", country_code="IN", country="India", logistic_name="", from_country_code="", lines=[SupplierLine(vid=vid, quantity=2)])


def test_printful_catalog_order_and_status():
    def prod(m, u, h, b):
        return {"result": {"sync_product": {"id": 11}, "sync_variants": [{"id": 501, "name": "M / Black", "sku": "T-M", "product": {"variant_id": 4012}}]}}
    posted = {}

    def order(m, u, h, b):
        posted.update(json.loads(b))
        posted["url"] = u
        return {"result": {"id": 9001, "status": "pending"}}
    AS.HTTP = Net({"/store/products/11": prod, "/store/products?": {"result": [{"id": 11, "name": "Tee", "thumbnail_url": "https://t/1.png"}], "paging": {"total": 1}},
                   "/products/variant/4012": {"result": {"variant": {"price": "9.95"}}}, "/orders?confirm=true": order,
                   "/orders/9001": {"result": {"id": 9001, "status": "fulfilled", "shipments": [{"tracking_number": "TRK1", "carrier": "DHL"}]}},
                   "/orders/@W404": (404, b"{}")})
    pf = pod.PrintfulSupplier({}, {"api_token": "tok"})
    items = pf.list_catalog()
    assert items[0].vid == "501" and items[0].cost == Decimal("9.95")  # cost = Printful's base price, never a guess
    res = pf.create_order(_req("501"))
    assert res.supplier_order_id == "9001" and posted["external_id"] == "W1001" and posted["items"] == [{"sync_variant_id": 501, "quantity": 2}] and "confirm=true" in posted["url"]
    info = pf.get_order_info("9001")
    assert (info.status, info.tracking_number, info.carrier) == ("shipped", "TRK1", "DHL")
    assert pf.find_order("W404") is None


def test_printful_cost_unknown_is_an_error_not_a_guess():
    AS.HTTP = Net({"/products/variant/4012": {"result": {"variant": {}}}})
    with pytest.raises(SupplierError):
        pod.PrintfulSupplier({}, {"api_token": "t"}).base_cost({"product": {"variant_id": 4012}})


def test_printify_catalog_order_and_status():
    sent = {}

    def order(m, u, h, b):
        sent.update(json.loads(b))
        return {"id": "ord1", "status": "pending"}
    AS.HTTP = Net({"/shops.json": [{"id": 77, "title": "S"}],
                   "/shops/77/products.json": {"total": 1, "data": [{"id": "p1", "title": "Mug", "images": [{"src": "https://i/m.png", "is_default": True}],
                                                                       "variants": [{"id": 3, "title": "11oz", "cost": 450, "is_enabled": True, "is_available": True}, {"id": 4, "title": "off", "cost": 500, "is_enabled": False}]}]},
                   "/shops/77/orders.json": order, "/send_to_production.json": {}, "/shops/77/orders/ord1.json": {"id": "ord1", "status": "fulfilled", "shipments": [{"number": "Z9", "carrier": "UPS"}]}})
    pr = pod.PrintifySupplier({}, {"api_token": "tok"})
    items = pr.list_catalog()
    assert [(i.vid, i.cost) for i in items] == [("p1:3", Decimal("4.50"))]
    pr.create_order(_req("p1:3"))
    assert sent["line_items"] == [{"product_id": "p1", "variant_id": 3, "quantity": 2}] and sent["address_to"]["first_name"] == "Asha" and sent["address_to"]["last_name"] == "Rao"
    assert pr.get_order_info("ord1").status == "shipped"
    with pytest.raises(SupplierError):
        pr.create_order(_req("badvid"))
    assert all(c[2].get("User-Agent") for c in AS.HTTP.calls)  # Printify refuses requests without one


# ---- the 30 programs ---------------------------------------------------------------------------
def test_thirty_programs_are_real_and_honest(client):
    assert len(PROGRAMS) == 30 and len({p.key for p in PROGRAMS}) == 30
    mgr = client.app.state.connectors
    for p in PROGRAMS:
        t = mgr.type(p.key)
        assert t is not None and t.program is p
        assert p.howto and p.verified and p.commission
        assert p.top_pct is None or p.src, f"{p.key}: a commission number needs a source"
    tops = [p.top_pct for p in ranked() if p.top_pct is not None]
    assert tops == sorted(tops, reverse=True) and ranked()[0].key == "aff_digistore24"
    assert {p.group for p in PROGRAMS} >= {"network", "marketplace", "digital", "pod_api", "pod_market"}
    assert by_key("pod_printful").mode == "printful" and mgr.type("pod_printful").kind == "supplier" and mgr.type("aff_amazon").kind == "affiliate"


def test_every_connect_form_opens_and_programs_page_renders(client):
    login(client)
    for p in PROGRAMS:
        r = client.get(f"/connect/new?type={p.key}")
        assert r.status_code == 200 and p.label.split(" (")[0] in r.text.replace("&amp;", "&"), p.key
    html = client.get("/programs").text
    assert all(p.label.split(" (")[0].replace("&", "&amp;") in html or p.label.split(" (")[0] in html for p in PROGRAMS)
    assert "Digistore24" in html and "Sach:" in html


# ---- storefront, clicks, agents ---------------------------------------------------------------
def _connect_feed(client, tok, routes):
    AS.HTTP = Net(routes)
    r = post(client, "/connect/new", tok, type="aff_feed", name="Test feed", feed_url="https://feed.example/f.csv", merchant="MugShop", currency="USD")
    assert r.status_code == 303
    client.get("/programs")  # shows (and clears) the 'saved' flash
    with client.app.state.database.SessionLocal() as db:
        c = db.query(Connection).filter(Connection.type == "aff_feed").one()
        assert c.last_status == "ok", c.last_error
        return c.id


def test_import_shop_go_click_and_no_cart(client):
    tok = login(client)
    cid = _connect_feed(client, tok, {"feed.example": AWIN})
    r = post(client, f"/programs/{cid}/import", tok, keywords="mug", limit="5")
    assert r.status_code == 303
    items = client.get("/shop/products").json()["products"]
    assert len(items) == 2 and all(i["affiliate"] and i["merchant"] == "MugShop" and i["go"].startswith("/shop/go/") for i in items)
    assert "awin1.com" not in json.dumps(items)  # the raw merchant link is never sent to the browser, only our tracked /shop/go address
    pid = items[0]["id"]
    g = client.get(f"/shop/go/{pid}")
    assert g.status_code == 302 and g.headers["location"].startswith("https://www.awin1.com/pclick.php") and g.headers["cache-control"] == "no-store"
    client.get(f"/shop/go/{pid}")
    with client.app.state.database.SessionLocal() as db:
        p = db.get(Product, pid)
        assert p.clicks == 2 and p.kind == "affiliate" and p.stock == 999 and p.supplier_price == 0
        p.active = False
        db.commit()
    assert client.get(f"/shop/go/{pid}").status_code == 404  # an inactive product never redirects
    assert client.get("/shop/go/999999").status_code == 404
    q = client.post("/shop/quote", json={"items": [{"id": items[1]["id"], "qty": 1}]})
    assert q.status_code in (400, 422) and "partner" in q.text.lower()


def test_agent_refreshes_prices_hides_dead_links_and_stock_sync_ignores_affiliate(client):
    tok = login(client)
    cid = _connect_feed(client, tok, {"feed.example": AWIN})
    post(client, f"/programs/{cid}/import", tok, keywords="", limit="5")
    new_feed = AWIN.replace(b"12.50", b"14.00").replace(b"2,Red Mug,9.00,https://www.awin1.com/pclick.php?p=2,https://i.example/2.jpg,Red mug,Home,USD\n", b"")
    AS._feed_cache.clear()
    AS.HTTP = Net({"feed.example": new_feed})
    rt = client.app.state.rt
    with client.app.state.database.SessionLocal() as db:
        res = AF.run(db, rt)
        assert res["repriced"] == 1 and res["hidden"] == 1 and res["errors"] == 0
        rows = {p.supplier_vid: p for p in db.query(Product).filter(Product.kind == "affiliate")}
        assert rows["1"].selling_price == Decimal("14.00") and rows["2"].auto_hidden and rows["2"].hidden_reason == "Feed se hat gaya"
        from app.services import sync
        stats = sync.stock_price_sync(db, rt)
        assert stats["checked"] == 0 and stats["errors"] == 0  # affiliate products are never asked of a supplier
        assert db.get(Product, rows["1"].id).selling_price == Decimal("14.00")
    AS._feed_cache.clear()
    AS.HTTP = Net({"feed.example": new_feed.replace(b"Blue Mug", b"Blue Mug 2")})
    with client.app.state.database.SessionLocal() as db:  # a broken program is reported, others keep working
        AS.HTTP = Net({"feed.example": (500, b"x")})
        AS._feed_cache.clear()
        assert AF.run(db, rt)["errors"] == 1
        assert db.get(Connection, cid).last_status == "error"


def test_foreign_currency_needs_a_rate_and_is_converted(client):
    tok = login(client)
    feed = b"id,name,price,link,currency\n1,Kurta,1500,https://m/k,INR\n2,Odd,10,https://m/o,XYZ\n"
    cid = _connect_feed(client, tok, {"feed.example": feed})
    with client.app.state.database.SessionLocal() as db:
        fx.ensure_seed(db)
        db.commit()
    post(client, f"/programs/{cid}/import", tok, keywords="", limit="5")
    with client.app.state.database.SessionLocal() as db:
        rows = db.query(Product).filter(Product.kind == "affiliate").all()
        assert [r.title for r in rows] == ["Kurta"] and 0 < rows[0].selling_price < 100  # XYZ has no rate: skipped, not guessed


def test_storefront_js_has_buy_link_and_disclosure():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "api" / "app" / "storefront"
    js, html = (root / "app.js").read_text(), (root / "index.html").read_text()
    assert "p.affiliate" in js and 'rel="sponsored nofollow noopener"' in js and "commission" in js and "partner links" in html
