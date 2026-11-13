"""Customer tickets, feedback and verified-buyer reviews."""
from __future__ import annotations

import pytest

from app.models import Alert, Review, Ticket
from app.services import mailer, shop
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_shop import ADDR, order_body, seed
from tests.test_web import login, post


@pytest.fixture(autouse=True)
def _fresh():
    shop._hits.clear()
    shop.drop_cache()
    yield


def paid_order(client):
    pid = seed(client)
    j = client.post("/shop/orders", json=order_body(pid)).json()
    tok = login(client)
    post(client, "/website/orders/1/paid", tok)
    return pid, j, tok


def test_anyone_can_open_a_ticket_and_only_the_token_opens_it(client):
    r = client.post("/shop/tickets", json={"kind": "question", "name": "Ravi", "email": "ravi@example.com", "message": "Do you ship to Goa?"})
    assert r.status_code == 201
    tk = r.json()["token"]
    th = client.get(f"/shop/tickets/{tk}").json()
    assert th["status"] == "open" and th["messages"][0]["body"] == "Do you ship to Goa?" and th["order"] == ""
    assert client.get("/shop/tickets/wrongtoken").status_code == 404
    with client.app.state.database.session() as db:
        assert db.query(Alert).filter(Alert.source == "support").count() == 1


def test_ticket_validation_honeypot_and_order_matching(client):
    pid, j, tok = paid_order(client)
    base = {"kind": "issue", "name": "Asha", "email": ADDR["email"], "message": "My parcel is late"}
    assert client.post("/shop/tickets", json={**base, "email": "nope"}).status_code == 422
    assert client.post("/shop/tickets", json={**base, "message": "hi"}).status_code == 422
    assert client.post("/shop/tickets", json={**base, "website": "http://spam"}).status_code == 422  # bots fill the hidden field
    wrong = client.post("/shop/tickets", json={**base, "order": "W1001", "email": "other@example.com"})
    assert wrong.status_code == 422 and "match" in wrong.json()["error"]
    ok = client.post("/shop/tickets", json={**base, "order": j["number"]})
    assert ok.status_code == 201
    with client.app.state.database.session() as db:
        t = db.query(Ticket).one()
        assert t.order_number == j["number"] and t.shop_order_id == 1


def test_reply_flow_and_reopening(client):
    tk = client.post("/shop/tickets", json={"kind": "issue", "name": "A", "email": "a@example.com", "message": "Item arrived broken"}).json()
    tok = login(client)
    page = client.get("/support")
    assert page.status_code == 200 and "Item arrived broken" in page.text
    assert post(client, "/support/1/reply", tok, message="Sorry! We will send a new one.").status_code == 303
    th = client.get(f"/shop/tickets/{tk['token']}").json()
    assert th["status"] == "answered" and [m["who"] for m in th["messages"]] == ["customer", "admin"]
    assert client.post(f"/shop/tickets/{tk['token']}/reply", json={"message": "Thank you, sending photo"}).status_code == 200
    assert client.get(f"/shop/tickets/{tk['token']}").json()["status"] == "open"  # a customer reply reopens it
    post(client, "/support/1/status", tok, status="closed")
    with client.app.state.database.session() as db:
        assert db.query(Ticket).one().status == "closed"


def test_reply_emails_the_customer_with_a_link_when_a_mailbox_is_connected(client):
    sent = []
    mailer.SENDER = sent.append
    try:
        tok = login(client)
        post(client, "/website/settings", tok, shop_site_url="https://shop.example.com", shop_currencies="USD", shop_countries="US", shop_ship_flat="1", shop_free_over="0", shop_on="on")
        from app.services import settings_service as S
        with client.app.state.database.session() as db:
            S.set_value(db, "customer_emails", "true")
            db.commit()
        tk = client.post("/shop/tickets", json={"kind": "question", "name": "A", "email": "a@example.com", "message": "Is it waterproof?"}).json()
        post(client, "/support/1/reply", tok, message="Yes, IPX5.")
    finally:
        mailer.SENDER = None
    assert any(f"https://shop.example.com/#ticket={tk['token']}" in m.get_content() for m in sent), sent
    assert all(m["To"] == "a@example.com" for m in sent) and len(sent) >= 2  # an acknowledgement and the reply


def test_reviews_need_a_paid_order_and_your_approval(client):
    pid, j, tok = paid_order(client)
    base = {"product_id": pid, "order": j["number"], "email": ADDR["email"], "rating": 5, "title": "Great", "body": "Works really well, fast delivery."}
    assert client.post("/shop/reviews", json={**base, "email": "other@example.com"}).status_code == 422
    assert client.post("/shop/reviews", json={**base, "rating": 9}).status_code == 422
    assert client.post("/shop/reviews", json={**base, "product_id": 999}).status_code == 422
    assert client.post("/shop/reviews", json=base).status_code == 201
    assert client.post("/shop/reviews", json=base).status_code == 422  # once per product per order
    assert client.get(f"/shop/reviews/{pid}").json()["reviews"] == []  # hidden until approved
    shop.drop_cache()
    assert client.get("/shop/products").json()["products"][0]["reviews"] == 0
    post(client, "/support/reviews/1/approve", tok)
    shop.drop_cache()
    assert [r["title"] for r in client.get(f"/shop/reviews/{pid}").json()["reviews"]] == ["Great"]
    p = client.get("/shop/products").json()["products"][0]
    assert p["rating"] == 5.0 and p["reviews"] == 1
    with client.app.state.database.session() as db:
        assert db.query(Review).one().status == "approved"


def test_an_unpaid_order_cannot_review(client):
    pid = seed(client)
    j = client.post("/shop/orders", json=order_body(pid)).json()
    r = client.post("/shop/reviews", json={"product_id": pid, "order": j["number"], "email": ADDR["email"], "rating": 5, "body": "Looks nice and good"})
    assert r.status_code == 422


def test_unanswered_tickets_raise_an_alert_after_a_day(client):
    from datetime import timedelta
    from app.db import utcnow
    client.post("/shop/tickets", json={"kind": "issue", "name": "A", "email": "a@example.com", "message": "Nobody answers"})
    with client.app.state.database.session() as db:
        db.query(Ticket).one().updated_at = utcnow() - timedelta(hours=30)
        db.commit()
        assert shop.watch(db, client.app.state.rt)["overdue"] == 1
        assert db.query(Alert).filter(Alert.dedupe_key == "tickets-overdue").count() == 1


def test_tickets_are_rate_limited(client):
    codes = [client.post("/shop/tickets", json={"kind": "question", "name": "A", "email": "a@example.com", "message": "hello there"}).status_code for _ in range(7)]
    assert codes.count(201) == 5 and 429 in codes
