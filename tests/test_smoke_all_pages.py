"""Every page and every simple GET route must answer without a server error, logged in and logged out."""
from __future__ import annotations

import re

from tests.test_web import client, login, web_cfg  # noqa: F401  (fixtures)


def _get_routes(app):
    paths = app.openapi().get("paths", {})
    skip = ("/static", "/internal", "/logout", "/docs", "/redoc", "/openapi")
    return sorted(p for p, ops in paths.items() if "get" in ops and "{" not in p and not p.startswith(skip))


def test_every_get_page_answers_without_server_error(client):  # noqa: F811
    login(client)
    paths = _get_routes(client.app)
    assert len(paths) > 25
    bad = []
    for p in paths:
        r = client.get(p)
        if r.status_code >= 500:
            bad.append((p, r.status_code, re.sub(r"\s+", " ", r.text)[:150]))
    assert not bad, bad


def test_every_get_page_logged_out_never_500(client):  # noqa: F811
    bad = [(p, client.get(p).status_code) for p in _get_routes(client.app) if client.get(p).status_code >= 500]
    assert not bad, bad


def test_pages_with_ids_answer_cleanly_for_missing_records(client):  # noqa: F811
    login(client)
    paths = [p for p, ops in client.app.openapi().get("paths", {}).items() if "get" in ops and "{" in p and not p.startswith(("/static", "/internal", "/docs"))]
    bad = []
    for p in paths:
        for val in ("1", "999999", "nope"):
            url = re.sub(r"\{[^}]+\}", val, p)
            r = client.get(url)
            if r.status_code >= 500:
                bad.append((url, r.status_code))
    assert not bad, bad
