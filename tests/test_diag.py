"""Logging you can debug with: secrets removed, same problem grouped, hints, crash -> Error ID, CLI commands logged, report download."""
from __future__ import annotations

import logging

import pytest

from app.models import DiagEvent
from app.services import diag, hostcli
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import login, post


def rows(client):
    diag.flush()
    with client.app.state.database.SessionLocal() as db:
        return db.query(DiagEvent).order_by(DiagEvent.id).all()


def test_redact_hides_every_kind_of_secret():
    t = diag.redact("db postgresql://u:PW123456@h/db  Authorization: Bearer abcdefgh12345  CJ1@api@abcdef12  --env-var SECRET_KEY=zzzzzzzz  "
                    "ghp_ABCDEFGHIJKLMNOPQRSTUV  DATABASE_URL=postgres://a:b@c/d  https://x/y?token=abc123&z=1  {\"password\": \"hunter22\"}")
    for leak in ("PW123456", "abcdefgh12345", "abcdef12", "zzzzzzzz", "ABCDEFGHIJKLMNOPQRSTUV", "a:b@c", "abc123", "hunter22"):
        assert leak not in t
    assert "postgresql://u:***@h/db" in t and "z=1" in t


def test_known_stored_secret_is_hidden_even_if_it_does_not_look_like_a_key(monkeypatch):
    monkeypatch.setattr(diag, "_known", ["plainwordsecret"])
    assert "plainwordsecret" not in diag.redact("value is plainwordsecret ok")


def test_log_lines_are_saved_without_secrets_and_with_category(client):
    log = logging.getLogger("cheapedeal.cli")
    log.warning("cli fail: token=SUPERSECRET99 postgres://u:DBPASS77@h/d", extra={"cat": "cli"})
    logging.getLogger("cheapedeal.misc").info("hello info")
    r = rows(client)
    bad = [x for x in r if "cli fail" in x.message][0]
    assert bad.level == "WARNING" and bad.category == "cli" and "SUPERSECRET99" not in bad.message and "DBPASS77" not in bad.message
    assert any(x.message == "hello info" and x.level == "INFO" for x in r)


def test_noisy_libraries_are_not_stored_but_their_errors_are(client):
    logging.getLogger("httpx").info("HTTP Request: GET https://x")
    logging.getLogger("sqlalchemy.engine").warning("some sql warning")
    logging.getLogger("somelib").warning("lib warning kept")
    msgs = [x.message for x in rows(client)]
    assert "lib warning kept" in msgs and not any("HTTP Request" in m or "some sql warning" in m for m in msgs)


def test_same_problem_is_grouped_and_gets_a_hint(client):
    for i in range(5):
        logging.getLogger("cheapedeal.cli").warning("cli fail exit 1 (%d.2s): render services | Error: no workspace set. Use `render workspace set`", i, extra={"cat": "cli"})
    diag.flush()
    with client.app.state.database.SessionLocal() as db:
        gs = diag.groups(db, "WARNING")
    g = [x for x in gs if "no workspace" in x["message"]]
    assert len(g) == 1 and g[0]["n"] == 5 and "workspace" in g[0]["hint"]["title"].lower() and "render workspace set" in g[0]["hint"]["fix"]


def test_flood_of_one_error_is_capped(client):
    for _ in range(200):
        logging.getLogger("cheapedeal.x").warning("same thing again")
    diag.flush()
    n = sum(1 for x in rows(client) if x.message == "same thing again")
    assert n <= 21 and diag.sink().dropped >= 179


def test_traceback_is_saved_and_fingerprint_ignores_numbers(client):
    for i in (1, 2):
        try:
            raise ValueError(f"bad order {i}")
        except ValueError:
            logging.getLogger("cheapedeal.engine").exception("order %d failed", i)
    r = [x for x in rows(client) if "failed" in x.message]
    assert len(r) == 2 and r[0].fingerprint == r[1].fingerprint and r[0].exc_type == "ValueError" and "Traceback" in r[0].trace and r[0].category == "engine"


def test_crash_in_a_page_returns_error_id_and_is_logged_with_trace(client):
    def boom():
        raise RuntimeError("kaboom secret=TOPSECRET123")
    client.app.add_api_route("/boom", boom, methods=["GET"])
    r = client.get("/boom", headers={"accept": "text/html"})
    assert r.status_code == 500 and "Error ID" in r.text
    rid = r.headers["x-request-id"]
    assert rid in r.text
    ev = [x for x in rows(client) if x.ref == rid]
    assert ev and ev[0].level == "ERROR" and ev[0].path == "/boom" and "RuntimeError" in ev[0].trace and "TOPSECRET123" not in ev[0].trace
    j = client.get("/boom")  # non-HTML caller gets json with the id
    assert j.status_code == 500 and j.json()["error_id"] == j.headers["x-request-id"]


def test_blocked_post_is_logged_as_security_warning(client):
    login(client)
    assert client.post("/orders/1/retry", data={}).status_code == 403  # logged in but no CSRF token
    assert any(x.category == "security" for x in rows(client))


def test_cli_commands_are_logged_with_result_and_tail(client, monkeypatch):
    monkeypatch.setattr(hostcli, "cmd_for", lambda which: ["render"])
    monkeypatch.setattr(hostcli, "RUN", lambda cmd, cwd=None, timeout=300: (1, "", "Error: no workspace set... postgres://u:CLIPW88@h/d"))
    with pytest.raises(hostcli.CliError):
        hostcli.render_workspaces()
    r = [x for x in rows(client) if x.category == "cli"]
    assert r and r[-1].level == "WARNING" and "render workspaces" in r[-1].message and "CLIPW88" not in r[-1].message


def test_diagnostics_page_report_and_admin_only(client):
    assert client.get("/diagnostics").status_code == 303
    tok = login(client)
    logging.getLogger("cheapedeal.cli").warning("cli fail exit 1 (1.0s): neon me | npm error code ETARGET", extra={"cat": "cli"})
    html = client.get("/diagnostics").text
    assert "ETARGET" in html and "neonctl@7.0.8" in html and "Health check" in html
    assert "ETARGET" in client.get("/diagnostics?view=stream").text
    rep = client.get("/diagnostics/download")
    assert rep.status_code == 200 and "attachment" in rep.headers["content-disposition"] and "== HEALTH ==" in rep.text and "ETARGET" in rep.text
    post(client, "/diagnostics/test", tok)
    assert "Test error" in client.get("/diagnostics?view=stream&q=Test").text
    post(client, "/diagnostics/clear", tok, mode="all")
    assert "Koi line nahi" in client.get("/diagnostics?view=stream").text


def test_prune_removes_old_rows_and_caps_total(client):
    from datetime import timedelta
    from app.db import utcnow
    with client.app.state.database.SessionLocal() as db:
        db.add(DiagEvent(created_at=utcnow() - timedelta(days=30), level="INFO", message="old"))
        for i in range(5):
            db.add(DiagEvent(level="INFO", message=f"new{i}"))
        db.commit()
        diag.prune(db, days=14, max_rows=3)
        left = [r.message for r in db.query(DiagEvent).order_by(DiagEvent.id)]
    assert "old" not in left and len(left) <= 3 and "new4" in left


def test_nav_shows_error_badge(client):
    login(client)
    logging.getLogger("cheapedeal.x").error("something broke")
    diag.flush()
    assert 'title="Errors in last 24h"' in client.get("/").text
