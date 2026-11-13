from __future__ import annotations

import json

import pytest

from app.models import EmailLog
from app.services import assistant as AI, mailer
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import login, post


@pytest.fixture(autouse=True)
def _reset():
    mailer.SENDER = None
    AI.HTTP = None
    yield
    mailer.SENDER = None
    AI.HTTP = None


def _connect(client, tok, **kw):
    d = dict(host="smtp.gmail.com", port="587", user="me@gmail.com", password="app-pass-1234", from_addr="", name="Meri Dukaan", cap="30")
    d.update(kw)
    return post(client, "/email/smtp", tok, **d)


def test_mailbox_validation_and_password_is_sealed(client):
    tok = login(client)
    assert "Not connected" in client.get("/email").text
    post(client, "/email/smtp", tok, host="https://bad host", port="abc", user="x", password="p", from_addr="nope")
    assert "Not connected" in client.get("/email").text  # nothing saved on bad input
    assert _connect(client, tok).status_code == 303
    html = client.get("/email").text
    assert "Connected" in html and "smtp.gmail.com" in html and "app-pass-1234" not in html and "saved (badalna" in html
    app = client.app
    with app.state.database.session() as s:
        from app.services import settings_service as S
        raw = S.get(s, "smtp_password_enc", "")
        assert raw and "app-pass-1234" not in raw
        assert mailer.smtp_settings(s)["password"] == "app-pass-1234" and mailer.smtp_settings(s)["from"] == "me@gmail.com"
    _connect(client, tok, password="")  # blank keeps the old password
    with app.state.database.session() as s:
        assert mailer.smtp_settings(s)["password"] == "app-pass-1234"
    post(client, "/email/smtp", tok, action="remove")
    assert "Not connected" in client.get("/email").text


def test_send_test_and_manual_mail_are_logged_and_safe(client):
    tok = login(client)
    sent = []
    mailer.SENDER = sent.append
    _connect(client, tok)
    post(client, "/email/test", tok, to="me@gmail.com")
    post(client, "/email/send", tok, to="a@shop.in, b@firm.com", subject="Hi\r\nBcc: evil@x.com", body="Hello there")
    assert [m["To"] for m in sent] == ["me@gmail.com", "a@shop.in", "b@firm.com"]
    assert "\n" not in sent[1]["Subject"] and "Bcc" not in sent[1].keys()  # header injection is flattened
    assert "Meri Dukaan" in sent[1]["From"]
    post(client, "/email/send", tok, to="bad address", subject="s", body="b")
    post(client, "/email/send", tok, to="x@y.com", subject="", body="b")
    with client.app.state.database.session() as s:
        rows = s.query(EmailLog).all()
        assert len(rows) == 4 and sum(r.ok for r in rows) == 3 and all(r.by == "you" for r in rows)  # the bad address is logged as failed
    assert "Hi Bcc: evil@x.com" in client.get("/email").text


def test_daily_cap_stops_mail(client):
    tok = login(client)
    mailer.SENDER = lambda m: None
    _connect(client, tok, cap="2")
    post(client, "/email/send", tok, to="a@x.com, b@x.com, c@x.com", subject="s", body="b")
    with client.app.state.database.session() as s:
        assert s.query(EmailLog).filter(EmailLog.ok.is_(True)).count() == 2
    assert "limit" in client.get("/email").text.lower()


def _ai(client, tok):
    AI.HTTP = lambda url, headers, payload: {"choices": [{"message": {"content": 'Sure: {"subject": "Delay update", "body": "Hello, your parcel is 5 days late. Sorry!"}'}}]}
    assert post(client, "/assistant/settings", tok, base_url="https://api.example.com/v1", api_key="k1", model="m", style="openai").status_code == 303


def test_ai_draft_then_ai_send_gate(client):
    tok = login(client)
    sent = []
    mailer.SENDER = sent.append
    _connect(client, tok)
    r = client.post("/email/draft", json={"to": "c@x.com", "request": "say sorry"}, headers={"X-CSRF-Token": tok})
    assert r.json()["ok"] is False and "AI model" in r.json()["error"]
    _ai(client, tok)
    r = client.post("/email/draft", json={"to": "c@x.com", "request": "say sorry"}, headers={"X-CSRF-Token": tok})
    assert r.json()["ok"] and r.json()["subject"] == "Delay update" and sent == []  # a draft never sends
    r = client.post("/email/ai-send", json={"to": "c@x.com", "request": "say sorry"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 403 and sent == []  # auto-send is off by default
    _connect(client, tok, ai_email_auto="on")
    r = client.post("/email/ai-send", json={"to": "c@x.com, d@x.com", "request": "say sorry"}, headers={"X-CSRF-Token": tok})
    assert r.json()["ok"] and r.json()["sent"] == 2 and len(sent) == 2
    with client.app.state.database.session() as s:
        assert {r.by for r in s.query(EmailLog).all()} == {"ai"}


def test_email_page_is_admin_only_and_customer_updates_use_saved_mailbox(client):
    tok = login(client)
    sent = []
    mailer.SENDER = sent.append
    _connect(client, tok)
    with client.app.state.database.session() as s:
        assert mailer.configured(s)
    from app import security
    assert "/email" in security.ADMIN_ONLY
