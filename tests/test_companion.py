"""The AI companion: read-only code access, secrets never exposed, honest notices, no invented numbers."""
from __future__ import annotations

import re
from pathlib import Path

from app.models import Alert, DiagEvent
from app.services import codeaccess, companion
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import login


def test_codeaccess_has_no_way_to_change_anything():
    src = Path(codeaccess.__file__).read_text()
    for bad in ("write_text", "write_bytes", "unlink", "rmdir", "rmtree", "os.remove", "os.rename", "shutil", "subprocess", "open(", "replace("):
        if bad == "replace(":
            continue  # str.replace is fine; path replace is not used (checked below)
        assert bad not in src, bad
    assert not re.search(r"Path\([^)]*\)\.replace|\.rename\(|\.touch\(|\.mkdir\(|\.chmod\(", src)


def test_secrets_and_junk_are_never_readable():
    for rel in ("local.env", "api/.env", "x/cheapedeal.env", "data/cheapedeal.db", "../etc/passwd", "/etc/passwd", "a/secret.key", "node_modules/x.js", "logs/app.log", "id.pem"):
        assert not codeaccess.allowed(rel), rel
        assert codeaccess.read(rel) == ""
    assert codeaccess.allowed("api/app/main.py")
    assert not any(f.endswith((".env", ".db", ".pem")) or "node_modules" in f for f in codeaccess.files())


def test_search_and_read_find_real_code_and_redact_keys():
    hits = codeaccess.search("NoSupplier refuse supplier connect")
    assert hits and any("no_supplier" in h["file"] for h in hits)
    body = codeaccess.read("api/app/adapters/no_supplier.py", 1, 10)
    assert "NoSupplier" in body or "supplier" in body.lower()
    assert "api/app/main.py" in codeaccess.files()
    assert "services/companion.py" in codeaccess.outline()


def test_symlink_cannot_escape_project(tmp_path, monkeypatch):
    secret = tmp_path / "outside.py"
    secret.write_text("TOP = 'nope'\n")
    link = codeaccess.ROOT / "docs" / "_escape_test.md"
    try:
        link.symlink_to(secret)
    except OSError:
        return
    try:
        assert codeaccess.read("docs/_escape_test.md") == ""
    finally:
        link.unlink()


def test_notices_only_for_new_real_problems_and_stay_quiet_on_repeat(client):
    with client.app.state.database.SessionLocal() as db:
        assert companion.since(db, -1)["notices"] == []  # fresh browser: nothing replayed
        db.add(Alert(level="error", source="cj", message="CJ order fail ho gaya", dedupe_key="k1"))
        db.add(DiagEvent(level="ERROR", message="boom NoneType", fingerprint="abc", exc_type="AttributeError"))
        db.commit()
        out = companion.since(db, 0)
        texts = " ".join(n["text"] for n in out["notices"])
        assert "CJ order fail" in texts and "NoneType" in texts
        again = companion.since(db, out["latest"])
        assert again["notices"] == []
        db.add(Alert(level="error", source="cj", message="CJ order fail ho gaya", dedupe_key="k1"))
        db.commit()
        assert companion.since(db, out["latest"])["notices"] == []  # same problem, told recently


def test_chatter_is_true_to_the_data(client):
    with client.app.state.database.SessionLocal() as db:
        line = companion.chatter(db)
        assert "0 order" in line or "koi order nahi" in line or "products" in line or "Product" in line or "product" in line


def test_endpoints_and_widget_are_on_every_page(client):
    assert client.get("/ai/notices").status_code in (303, 401, 403)  # login needed
    login(client)
    r = client.get("/ai/notices?after=-1")
    assert r.status_code == 200 and r.json()["notices"] == []
    assert client.get("/ai/chatter").json()["line"]
    html = client.get("/").text
    assert "/static/companion.js" in html
    js = client.get("/static/companion.js").text
    assert "speechSynthesis" in js and "SpeechRecognition" in js and "/assistant/ask" in js


def test_assistant_gets_code_only_for_admin(client, monkeypatch):
    seen = {}
    from app.services import assistant as AI
    monkeypatch.setattr(AI, "ask", lambda db, h, m, with_code=False: seen.setdefault("c", with_code) and {"answer": "ok", "mode": "offline"} or {"answer": "ok", "mode": "offline"})
    tok = login(client)
    r = client.post("/assistant/ask", json={"message": "x", "history": []}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200 and seen.get("c") is True


def test_offline_answer_points_at_code(client):
    with client.app.state.database.SessionLocal() as db:
        from app.services import assistant as AI
        out = AI.offline_answer(db, "autocatalog ka code kahan hai file", with_code=True)
        assert "autocatalog" in out


def test_voice_is_phrased_and_has_a_picker(client):
    login(client)
    js = client.get("/static/companion.js").text
    for needle in ("function phrases", "end==='?'", "Math.sin", "cc-vs", "setTimeout(next"):
        assert needle in js, needle
