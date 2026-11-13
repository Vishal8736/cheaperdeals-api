"""Platforms page + Flipkart affiliate price check (HTTP faked; real shapes follow Flipkart's docs, unverified live)."""
from __future__ import annotations

import httpx
import pytest

from app.services import shopcfg
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import login, post

seen: list[httpx.Request] = []

FK = {"productInfoList": [{"productBaseInfo": {"productIdentifier": {"productId": "P1"}, "productAttributes": {
    "title": "Neck Massager", "maximumRetailPrice": {"amount": 1999, "currency": "INR"}, "sellingPrice": {"amount": 899, "currency": "INR"}, "productUrl": "https://dl.flipkart.com/x", "inStock": True}}}]}


@pytest.fixture(autouse=True)
def fake():
    seen.clear()

    def h(req):
        seen.append(req)
        if "affiliate-api.flipkart.net" in str(req.url):
            if req.headers.get("fk-affiliate-token") != "tok-GOOD-1":
                return httpx.Response(401)
            return httpx.Response(200, json=FK)
        return httpx.Response(404)
    shopcfg.HTTP = httpx.Client(transport=httpx.MockTransport(h))
    yield
    shopcfg.HTTP = None


def test_page_is_honest_about_amazon_and_flipkart(client):
    login(client)
    html = client.get("/platforms").text
    assert "Amazon Seller" in html and "Flipkart Affiliate" in html and "possible nahi" in html and "abhi nahi bana" in html


def test_needs_login(client):
    assert client.get("/platforms").status_code == 303


def test_flipkart_key_sealed_and_search_sends_headers(client):
    tok = login(client)
    post(client, "/platforms/flipkart", tok, affid="myaff", token="tok-GOOD-1")
    html = client.get("/platforms?q=neck+massager").text
    assert "tok-GOOD-1" not in html and "Neck Massager" in html and "899" in html
    r = seen[-1]
    assert r.headers["fk-affiliate-id"] == "myaff" and r.url.params["query"] == "neck massager" and r.url.params["resultCount"] == "10"


def test_bad_token_gives_clear_message(client):
    tok = login(client)
    post(client, "/platforms/flipkart", tok, affid="myaff", token="wrong")
    assert "token/ID mana kar diya" in client.get("/platforms?q=x").text


def test_search_without_key_is_refused(client):
    login(client)
    html = client.get("/platforms?q=x").text
    assert "Flipkart price check" in html and "key nahi" in html
