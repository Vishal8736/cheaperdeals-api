"""The public website API: server-side pricing, payment gates, engine hand-off, keep-alive, admin page."""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

import httpx

from app.models import Order, Product, ShopOrder, Subscriber
from app.services import fx, payments, shop, shopcfg, supplies
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import login, post


import pytest


@pytest.fixture(autouse=True)
def _fresh_limits():
    shop._hits.clear()
    shop.drop_cache()
    yield


def seed(client, **over):
    with client.app.state.database.session() as db:
        db.add(Product(store_product_id=over.pop("spid", "web-1"), sku="SKU1", title=over.pop("title", "Lamp"), selling_price=Decimal(over.pop("price", "10")),
                       supplier_price=Decimal("4"), stock=over.pop("stock", 5), listing_status=over.pop("ls", "listed"), supplier_vid="v1", **over))
        db.commit()
        shop.drop_cache()
        return [p.id for p in db.query(Product).all()][-1]


ADDR = dict(name="Asha Rao", email="asha@example.com", phone="+91 98765 43210", address1="12 MG Road", city="Pune", state="MH", zip="411001", country="IN")


def order_body(pid, qty=1, **over):
    return {"items": [{"id": pid, "qty": qty}], **ADDR, **over}


def test_ping_needs_no_login_and_no_database(client):
    r = client.get("/ping")
    assert r.status_code == 200 and r.text == "ok" and "no-store" in r.headers["cache-control"]
    assert client.head("/ping").status_code == 200


def test_products_are_public_cached_and_only_sellable(client):
    a = seed(client)
    seed(client, spid="web-2", title="Hidden draft", ls="draft")
    seed(client, spid="web-3", title="Sold out", stock=0)
    seed(client, spid="web-4", title="No price", price="0")
    r = client.get("/shop/products")
    assert r.status_code == 200 and [p["title"] for p in r.json()["products"]] == ["Lamp"]
    assert "s-maxage" in r.headers["cache-control"]
    assert "supplier_price" not in r.text and "supplier_vid" not in r.text  # margins never leave the server
    again = client.get("/shop/products", headers={"if-none-match": r.headers["etag"]})
    assert again.status_code == 304
    cfg = client.get("/shop/config").json()
    assert cfg["name"] and cfg["methods"][0]["id"] == "manual" and "razorpay_key" in cfg and "USD" in cfg["rates"]
    assert r.json()["products"][0]["price_usd"] == "10.00"


def test_prices_come_from_the_server_not_the_browser(client):
    pid = seed(client, price="10")
    body = order_body(pid, 2)
    body["items"][0]["price"] = "0.01"  # a cheating browser
    body["total"] = "0.01"
    r = client.post("/shop/orders", json=body)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["total"] == "24.99" and j["method"] == "manual" and j["number"].startswith("W")  # 2 x 10 + 4.99 shipping
    free = client.post("/shop/orders", json=order_body(pid, 4)).json()  # 40 >= 35: free shipping
    assert free["total"] == "40.00"


def test_bad_orders_are_refused_with_a_clear_message(client):
    pid = seed(client, stock=2)
    bad = [order_body(pid, 3), order_body(pid, 0), order_body(99999), order_body(pid, email="nope"), order_body(pid, phone="12"),
           order_body(pid, country="XX"), order_body(pid, name=""), {"items": [], **ADDR}]
    for b in bad:
        shop._hits.clear()
        r = client.post("/shop/orders", json=b)
        assert r.status_code == 422 and r.json()["error"], b
    shop._hits.clear()
    assert client.post("/shop/orders", content=b"not json", headers={"content-type": "application/json"}).status_code == 400
    with client.app.state.database.session() as db:
        assert db.query(ShopOrder).count() == 0


def test_a_pending_order_never_reaches_the_engine_until_paid(client):
    pid = seed(client)
    n = client.post("/shop/orders", json=order_body(pid)).json()["number"]
    with client.app.state.database.session() as db:
        assert db.query(Order).count() == 0 and db.query(ShopOrder).one().status == "pending"
    tok = login(client)
    sid = 1
    assert post(client, f"/website/orders/{sid}/paid", tok).status_code == 303
    with client.app.state.database.session() as db:
        so = db.query(ShopOrder).one()
        assert so.status == "paid" and so.engine_order_id
        o = db.get(Order, so.engine_order_id)
        assert o.store_order_id == "web-1" and o.items[0].quantity == 1 and o.customer_email == "asha@example.com" and o.items[0].product_id
    post(client, f"/website/orders/{sid}/paid", tok)  # pressing twice must not create a second order
    with client.app.state.database.session() as db:
        assert db.query(Order).count() == 1


def test_cancel_only_works_on_pending(client):
    pid = seed(client)
    client.post("/shop/orders", json=order_body(pid))
    tok = login(client)
    post(client, "/website/orders/1/cancel", tok)
    with client.app.state.database.session() as db:
        assert db.query(ShopOrder).one().status == "cancelled"
        so = db.query(ShopOrder).one()
        assert shop.mark_paid(db, client.app.state.rt, so)[0] is None  # a cancelled order cannot be paid later


SETTINGS = dict(shop_ship_flat="4.99", shop_free_over="35", shop_currency="USD", shop_currencies="USD,INR,EUR", shop_countries="IN,US", shop_on="on")


def razorpay_setup(client, tok):
    post(client, "/website/settings", tok, method_razorpay="on", method_manual="on", razorpay_key_id="rzp_test_abc", razorpay_key_secret="SECRET123", razorpay_webhook_secret="WHSEC", **SETTINGS)


def test_razorpay_flow_checks_the_signature(client):
    pid = seed(client)
    tok = login(client)
    razorpay_setup(client, tok)
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"id": "order_RZ1"})
    shopcfg.HTTP = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        r = client.post("/shop/orders", json=order_body(pid, method="razorpay", currency="INR"))
    finally:
        shopcfg.HTTP = None
    j = r.json()
    assert j["method"] == "razorpay" and j["pay"]["order_id"] == "order_RZ1" and j["pay"]["key"] == "rzp_test_abc"
    rate = fx.rate(client.app.state.database.SessionLocal(), "INR")
    assert j["currency"] == "INR" and seen["body"]["amount"] == int(Decimal(j["total"]) * 100) and seen["body"]["currency"] == "INR" and seen["auth"].startswith("Basic ")
    with client.app.state.database.session() as db:
        assert "SECRET123" not in "".join(s.value for s in db.query(__import__("app.models", fromlist=["Setting"]).Setting).all())  # sealed
    bad = client.post(f"/shop/orders/{j['token']}/verify", json={"razorpay_order_id": "order_RZ1", "razorpay_payment_id": "pay_1", "razorpay_signature": "forged"})
    assert bad.status_code == 400
    with client.app.state.database.session() as db:
        assert db.query(Order).count() == 0
    sig = hmac.new(b"SECRET123", b"order_RZ1|pay_1", hashlib.sha256).hexdigest()
    ok = client.post(f"/shop/orders/{j['token']}/verify", json={"razorpay_order_id": "order_RZ1", "razorpay_payment_id": "pay_1", "razorpay_signature": sig})
    assert ok.status_code == 200 and ok.json()["ok"]
    with client.app.state.database.session() as db:
        assert db.query(Order).count() == 1 and db.query(ShopOrder).one().payment_id == "pay_1"


def test_razorpay_webhook_pays_checks_amount_and_signature(client):
    pid = seed(client)
    tok = login(client)
    razorpay_setup(client, tok)
    shopcfg.HTTP = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": "order_W1"})))
    try:
        j = client.post("/shop/orders", json=order_body(pid, method="razorpay", currency="INR")).json()
    finally:
        shopcfg.HTTP = None
    paise = int(Decimal(j["total"]) * 100)

    def send(amount, sig=None):
        raw = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_9", "order_id": "order_W1", "amount": amount}}}}).encode()
        s = sig or hmac.new(b"WHSEC", raw, hashlib.sha256).hexdigest()
        return client.post("/shop/razorpay/webhook", content=raw, headers={"x-razorpay-signature": s})
    assert send(paise, "bad").status_code == 401
    assert send(100).json()["ignored"] == "amount mismatch"
    with client.app.state.database.session() as db:
        assert db.query(Order).count() == 0
    assert send(paise).json()["ok"]
    send(paise)  # Razorpay retries: still one order
    with client.app.state.database.session() as db:
        assert db.query(Order).count() == 1


def test_track_needs_token_or_number_plus_email(client):
    pid = seed(client)
    j = client.post("/shop/orders", json=order_body(pid)).json()
    assert client.get("/shop/track", params={"ref": j["token"]}).json()["number"] == j["number"]
    by_num = client.get("/shop/track", params={"ref": j["number"], "email": "ASHA@example.com"})
    assert by_num.status_code == 200 and "token" not in by_num.json()
    assert client.get("/shop/track", params={"ref": j["number"], "email": "other@example.com"}).status_code == 404
    assert client.get("/shop/track", params={"ref": j["number"]}).status_code == 404


def test_subscribe_dedupes_and_validates(client):
    assert client.post("/shop/subscribe", json={"email": "A@x.com"}).status_code == 200
    assert client.post("/shop/subscribe", json={"email": "a@x.com"}).status_code == 200
    assert client.post("/shop/subscribe", json={"email": "bad"}).status_code == 422
    with client.app.state.database.session() as db:
        assert db.query(Subscriber).count() == 1


def test_orders_are_rate_limited(client):
    pid = seed(client)
    codes = [client.post("/shop/orders", json=order_body(pid)).status_code for _ in range(8)]
    assert 429 in codes


def test_closed_shop_takes_no_orders_and_admin_page_works(client):
    pid = seed(client, ls="draft")
    assert client.get("/website").status_code == 303  # login required
    tok = login(client)
    page = client.get("/website")
    assert page.status_code == 200 and "Website (dukaan)" in page.text and "/ping" in page.text
    post(client, "/website/publish", tok)
    assert [p["title"] for p in client.get("/shop/products").json()["products"]] == ["Lamp"]
    with client.app.state.database.session() as db:
        assert db.get(Product, pid).store_product_id == f"web-{pid}"
    post(client, "/website/settings", tok, **{**SETTINGS, "shop_on": ""})  # unchecked = closed
    r = client.post("/shop/orders", json=order_body(pid))
    assert r.status_code == 422 and "band" in r.json()["error"]
    assert client.get("/website/subscribers.csv").status_code == 200


def enable(client, tok, *methods, **extra):
    body = {**SETTINGS, "shop_currencies": "USD,INR,EUR", **extra}
    for m in methods:
        body["method_" + m] = "on"
    post(client, "/website/settings", tok, **body)


def test_prices_follow_the_chosen_currency_using_the_stored_rate(client):
    pid = seed(client, price="10")
    with client.app.state.database.session() as db:
        fx.ensure_seed(db)
        fx.set_rate(db, "INR", "80")
        fx.set_rate(db, "EUR", "0.9")
        db.commit()
    tok = login(client)
    enable(client, tok, "manual")
    cfg = client.get("/shop/config").json()
    assert cfg["rates"]["INR"]["rate"] == "80.000000" and cfg["rates"]["INR"]["symbol"] == "₹" and set(cfg["rates"]) == {"USD", "INR", "EUR"}
    q = client.post("/shop/quote", json={"items": [{"id": pid, "qty": 2}], "currency": "inr"}).json()
    assert q["currency"] == "INR" and q["subtotal"] == "1600.00" and q["shipping"] == "399.20" and q["total"] == "1999.20"  # 2 x 800 + 4.99 USD x 80
    assert client.post("/shop/quote", json={"items": [{"id": pid, "qty": 1}], "currency": "XXX"}).json()["currency"] == "USD"  # unknown -> default
    j = client.post("/shop/orders", json=order_body(pid, 2, currency="EUR")).json()
    assert j["currency"] == "EUR" and j["total"] == "22.49"  # 2 x 9.00 + 4.49
    with client.app.state.database.session() as db:
        so = db.query(ShopOrder).one()
        assert so.total_usd == Decimal("24.99") and so.currency == "EUR"
    post(client, "/website/orders/1/paid", tok)
    with client.app.state.database.session() as db:
        assert db.query(Order).one().total == Decimal("24.99")  # the engine books USD


def test_upi_is_inr_only_and_the_link_carries_the_exact_amount(client):
    pid = seed(client)
    tok = login(client)
    enable(client, tok, "upi", "manual", upi_vpa="shop@okbank", upi_name="Cheaper Deal")
    r = client.post("/shop/orders", json=order_body(pid, currency="USD", method="upi"))
    assert r.status_code == 422 and "INR" in r.json()["error"]
    j = client.post("/shop/orders", json=order_body(pid, currency="INR", method="upi")).json()
    assert j["method"] == "upi" and j["pay"]["link"].startswith("upi://pay?pa=shop%40okbank&pn=Cheaper%20Deal&am=") and f"am={j['total']}&cu=INR" in j["pay"]["link"]
    with client.app.state.database.session() as db:
        assert db.query(ShopOrder).count() == 1  # the refused attempt left nothing behind
    bad = client.post(f"/shop/orders/{j['token']}/utr", json={"utr": "12"})
    assert bad.status_code == 422
    assert client.post(f"/shop/orders/{j['token']}/utr", json={"utr": "4123 4567 8901"}).status_code == 200
    with client.app.state.database.session() as db:
        so = db.query(ShopOrder).one()
        assert so.status == "pending" and json.loads(so.pay_data)["utr"] == "412345678901"  # a typed UTR never pays an order by itself


def test_methods_that_are_not_set_up_stay_off(client):
    pid = seed(client)
    tok = login(client)
    enable(client, tok, "razorpay", "upi")  # nothing configured for either
    assert [m["id"] for m in client.get("/shop/config").json()["methods"]] == ["manual"]
    assert client.post("/shop/orders", json=order_body(pid, method="razorpay")).status_code == 422
    assert client.post("/shop/orders", json=order_body(pid, method="crypto")).status_code == 422  # crypto is gone for good


def test_refund_cancels_the_engine_order_and_calls_razorpay_when_it_was_razorpay(client):
    pid = seed(client)
    tok = login(client)
    enable(client, tok, "razorpay", "manual", razorpay_key_id="rzp_test_abc", razorpay_key_secret="SECRET123")
    calls = []

    def h(req: httpx.Request):
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json={"id": "order_R9"})
    shopcfg.HTTP = httpx.Client(transport=httpx.MockTransport(h))
    try:
        j = client.post("/shop/orders", json=order_body(pid, currency="USD", method="razorpay")).json()
        sig = hmac.new(b"SECRET123", b"order_R9|pay_7", hashlib.sha256).hexdigest()
        client.post(f"/shop/orders/{j['token']}/verify", json={"razorpay_order_id": "order_R9", "razorpay_payment_id": "pay_7", "razorpay_signature": sig})
        post(client, "/website/orders/1/refund", tok)
    finally:
        shopcfg.HTTP = None
    assert ("POST", "/v1/payments/pay_7/refund") in calls
    with client.app.state.database.session() as db:
        assert db.query(ShopOrder).one().status == "refunded" and db.query(Order).one().status == "cancelled"
        assert shop.mark_paid(db, client.app.state.rt, db.query(ShopOrder).one())[0] is None  # a refunded order cannot be paid again
    post(client, "/website/orders/1/refund", tok)  # second press: nothing happens, no second Razorpay call
    assert sum(1 for c in calls if c[1].endswith("/refund")) == 1


def test_unpaid_orders_close_after_48_hours(client):
    from datetime import timedelta
    from app.db import utcnow
    pid = seed(client)
    client.post("/shop/orders", json=order_body(pid))
    with client.app.state.database.session() as db:
        db.query(ShopOrder).one().created_at = utcnow() - timedelta(hours=49)
        db.commit()
        assert shop.watch(db, client.app.state.rt)["expired"] == 1
        assert db.query(ShopOrder).one().status == "cancelled"


def test_cj_wallet_balance_alerts_when_low(client):
    from app.models import Alert
    rt = client.app.state.rt

    class Sup:
        def get_balance(self):
            return Decimal("3.50")
    rt.supplier_for = lambda cid=None: Sup()
    with client.app.state.database.session() as db:
        note = supplies.cj_balance_check(db, rt, force=True)
        db.commit()
        assert note == "CJ balance $3.50" and db.query(Alert).filter(Alert.dedupe_key == "cj-low-balance").count() == 1
        assert supplies.cj_balance_check(db, rt) == ""  # checked less than 6 hours ago


def test_signature_helpers_reject_blank_and_wrong_values():
    assert not payments.webhook_hex_ok("", b"x", "abc") and not payments.webhook_hex_ok("s", b"x", "") and not payments.razorpay_signature_ok("s", "o", "p", "")
    assert payments.webhook_hex_ok("s", b"x", "sha256=" + payments.hmac_hex("s", b"x"))
