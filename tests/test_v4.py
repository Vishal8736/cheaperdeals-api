"""v4: Start/Stop engine, live refresh, Setup wizard, Playbook, AI assistant, Hinglish UI."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app import i18n
from app.db import utcnow
from app.models import Order, OrderItem, Product
from app.services import assistant as AI, engine as EN, playbook as PB, settings_service as S
from tests.test_web import client, csrf, login, post, web_cfg  # noqa: F401


# ---- engine ---------------------------------------------------------------------------------
def test_fresh_real_install_waits_for_start(tmp_path):
    from app.config import Config
    from app.db import Database
    c = Config(app_env="dev", database_url=f"sqlite:///{tmp_path}/e.db", secret_key="x" * 32, internal_api_key="k" * 32, admin_password="pw-for-tests")
    d = Database(c.database_url); d.create_all(); s = d.SessionLocal()
    S.ensure_defaults(s, c)
    assert S.get_bool(s, "paused") and not EN.running(s)


def test_start_stop(db):
    S.set_value(db, "paused", "true"); db.commit()
    EN.start(db); db.commit()
    assert EN.running(db) and S.get_bool(db, "playbook_enabled") and S.get_bool(db, "strategy_autopilot")
    EN.stop(db); db.commit()
    assert not EN.running(db)


def test_start_stop_buttons_and_live_header(client):
    tok = login(client)
    post(client, "/engine/stop", tok)
    assert "Start" in client.get("/").text
    post(client, "/engine/start", tok)
    assert "Stop" in client.get("/").text
    client.post("/lang", data={"csrf_token": tok, "lang": "en", "next": "/"})
    r = post(client, "/engine/stop", tok)
    assert r.status_code == 303
    live = client.get("/", headers={"X-Live": "1"})  # live polling must not eat the flash message
    assert 'class="flash' not in live.text
    assert 'class="flash' in client.get("/").text  # still waiting for the normal page view


def test_new_pages_render(client):
    tok = login(client)
    for url in ("/setup", "/playbook", "/assistant", "/guide"):
        assert client.get(url).status_code == 200, url
    assert client.get("/setup", follow_redirects=True).text


# ---- playbook -------------------------------------------------------------------------------
def test_playbook_validation(db):
    errs = PB.save(db, {"pb_tiers": "garbage", "pb_demand_days": "0"})
    assert errs
    assert not PB.save(db, {"pb_tiers": "0-20:80, 20+:30", "pb_demand_on": "on"})


def test_tiered_markup(db):
    S.set_value(db, "pb_tier_on", "true"); S.set_value(db, "pb_tiers", "0-20:80, 20+:30"); db.commit()
    assert PB.markup_for(db, Decimal("10"), Decimal("40")) == Decimal("80")
    assert PB.markup_for(db, Decimal("50"), Decimal("40")) == Decimal("30")
    S.set_value(db, "pb_tier_on", "false"); db.commit()
    assert PB.markup_for(db, Decimal("10"), Decimal("40")) == Decimal("40")


def _listed(db, product):
    product.listing_status = "listed"; product.listed_at = utcnow() - timedelta(days=60); product.stock = 10
    db.commit()


def test_playbook_clearance_never_below_floor_and_restores(db, rt, product):
    _listed(db, product)
    S.set_value(db, "paused", "false"); S.set_value(db, "playbook_enabled", "true"); db.commit()
    res = PB.run(db, rt)
    db.refresh(product)
    assert res["repriced"] == 1 and product.playbook_tag.startswith("clearance")
    assert Decimal(product.selling_price) < Decimal("25.00")
    assert Decimal(product.selling_price) >= PB._floor(db, product)
    # demand arrives -> clearance ends, price returns to the base
    for i in range(3):
        o2 = Order(store_order_id=f"d{i}", status="shipped", total=Decimal("25"), customer_name="A", customer_email="a@x.com")
        db.add(o2); db.flush()
        db.add(OrderItem(order_id=o2.id, product_id=product.id, store_product_id="P1", sku="SKU1", title="Desk Lamp", quantity=1, unit_price=Decimal("25")))
    db.commit()
    PB.run(db, rt)
    db.refresh(product)
    assert product.playbook_tag.startswith("demand") and Decimal(product.selling_price) > Decimal("25.00") - Decimal("0.01")


def test_playbook_respects_manual_and_pause(db, rt, product):
    _listed(db, product)
    S.set_value(db, "playbook_enabled", "true"); S.set_value(db, "paused", "true"); db.commit()
    assert PB.run(db, rt)["off"] is True
    S.set_value(db, "paused", "false"); product.manual_override = True; db.commit()
    PB.run(db, rt); db.refresh(product)
    assert Decimal(product.selling_price) == Decimal("25.00")


def test_playbook_sale_calendar(db):
    s = utcnow().date()
    line = f"Diwali|{s - timedelta(days=1)}|{s + timedelta(days=1)}|20"
    assert PB.parse_sale(line) and PB.parse_sale("bad line") is None
    S.set_value(db, "pb_sales", line); db.commit()
    assert PB.active_sale(db, s)[3] == 20


# ---- assistant ------------------------------------------------------------------------------
def test_assistant_offline_then_ai_both_styles(db, monkeypatch):
    AI.SECRET_KEY = "s" * 32
    assert AI.ask(db, [], "orders kaise approve karu?")["mode"] == "offline"
    calls = []

    def fake(url, headers, payload):
        calls.append((url, headers, payload))
        if "anthropic" in headers.get("anthropic-version", "x") or "x-api-key" in headers:
            return {"content": [{"type": "text", "text": "claude jawab"}]}
        return {"choices": [{"message": {"content": "openai jawab"}}]}
    monkeypatch.setattr(AI, "HTTP", fake)
    assert not AI.save_config(db, "https://api.example.com/v1", "sk-secret", "m1", "openai"); db.commit()
    assert "sk-secret" not in (S.get(db, "ai_key_enc", "") or "")  # sealed
    r = AI.ask(db, [], "hello")
    assert r["mode"] == "ai" and r["answer"] == "openai jawab" and calls[0][0].endswith("/chat/completions")
    assert calls[0][1]["Authorization"] == "Bearer sk-secret"
    assert not AI.save_config(db, "https://api.anthropic.com", "", "c1", "anthropic"); db.commit()
    r = AI.ask(db, [], "hello")
    assert r["answer"] == "claude jawab" and calls[-1][0].endswith("/v1/messages") and calls[-1][1]["x-api-key"] == "sk-secret"
    # customer details must not be sent to the model
    assert "asha@example.com" not in str(calls[-1][2])


def test_assistant_failure_falls_back(db, monkeypatch):
    AI.SECRET_KEY = "s" * 32
    AI.save_config(db, "https://api.example.com/v1", "k", "m", "openai"); db.commit()

    def boom(*a):
        raise AI.AIError("down")
    monkeypatch.setattr(AI, "HTTP", boom)
    r = AI.ask(db, [], "stock kaise sync hota hai")
    assert r["mode"] == "offline" and r["note"]


def test_assistant_validation(db):
    assert AI.save_config(db, "ftp://x", "", "m", "openai")
    assert AI.save_config(db, "https://x", "", "", "openai")


def test_assistant_routes_and_roles(client):
    tok = login(client)
    r = client.post("/assistant/ask", json={"message": "namaste", "history": []}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200 and r.json()["answer"]
    r = post(client, "/assistant/settings", tok, base_url="https://api.example.com/v1", api_key="k1", model="m", style="openai")
    assert r.status_code in (303, 200)


# ---- Hinglish -------------------------------------------------------------------------------
def test_translate_html():
    out = i18n.translate_html('<a>Orders</a><button>Save</button><input placeholder="Search..."><script>var a="Save";</script><textarea>Save</textarea><p>My Save here</p>')
    assert "Save karo" in out and 'placeholder="Khojo..."' in out
    assert 'var a="Save"' in out and "<textarea>Save</textarea>" in out and "My Save here" in out


def test_language_toggle(client):
    tok = login(client)
    client.post("/lang", data={"csrf_token": tok, "lang": "hi", "next": "/"})
    assert "Jodo (Connect)" in client.get("/connect").text or "Settings" in client.get("/").text
    assert "Naye orders" in client.get("/").text
    client.post("/lang", data={"csrf_token": tok, "lang": "en", "next": "/"})
    assert "Latest orders" in client.get("/").text


# ---- AI models: fetch list, multi-model auto, advisor -------------------------------------------
def test_list_models_openai_and_anthropic(monkeypatch):
    seen = []

    def get(url, headers):
        seen.append((url, headers))
        if "anthropic-version" in headers:
            return {"data": [{"id": "claude-a"}, {"id": "claude-b"}]}
        return {"data": [{"id": "gpt-4o"}, {"id": "text-embedding-3-small"}, {"id": "gpt-4o-mini"}]}
    monkeypatch.setattr(AI, "HTTP_GET", get)
    ms = AI.list_models("https://api.example.com/v1/", "sk", "openai")
    assert seen[0][0] == "https://api.example.com/v1/models" and seen[0][1]["Authorization"] == "Bearer sk"
    assert {m["id"]: m["chat"] for m in ms} == {"gpt-4o": True, "gpt-4o-mini": True, "text-embedding-3-small": False}
    ms = AI.list_models("https://api.anthropic.com", "k", "anthropic")
    assert seen[1][0].startswith("https://api.anthropic.com/v1/models") and [m["id"] for m in ms] == ["claude-a", "claude-b"]
    monkeypatch.setattr(AI, "HTTP_GET", lambda u, h: {"data": []})
    try:
        AI.list_models("https://x.com/v1", "k", "openai"); assert False
    except AI.AIError:
        pass
    try:
        AI.list_models("not-a-url", "k", "openai"); assert False
    except AI.AIError:
        pass


def test_multi_model_fallback_and_auto_rotation(db, monkeypatch):
    AI.SECRET_KEY = "s" * 32; AI._bad.clear(); AI._rr[0] = 0
    assert not AI.save_config(db, "https://api.example.com/v1", "k", "m1, m2 ,m3", "openai", auto=True); db.commit()
    assert AI.models_of(AI.config(db)) == ["m1", "m2", "m3"]
    used = []

    def fake(url, headers, payload):
        used.append(payload["model"])
        if payload["model"] == "m1":
            raise AI.AIError("limit")
        return {"choices": [{"message": {"content": "ok " + payload["model"]}}]}
    monkeypatch.setattr(AI, "HTTP", fake)
    assert AI.ask(db, [], "hi")["answer"] == "ok m2" and used == ["m1", "m2"]  # m1 failed -> m2 took over
    used.clear()
    assert AI.ask(db, [], "hi")["answer"] == "ok m2" and used == ["m2"]  # m1 rests for 10 minutes
    ok, msg = AI.test(db)
    assert ok and "m2" in msg and "m1" in msg
    # auto mode rotates the starting model for background work
    AI._bad.clear(); used.clear()
    c = AI.config(db)
    firsts = [AI._order(c, True)[0] for _ in range(3)]
    assert sorted(firsts) == ["m1", "m2", "m3"]


def test_advisor_review(db, monkeypatch):
    from app.models import AgentEvent
    AI.SECRET_KEY = "s" * 32; AI._bad.clear()
    assert AI.review(db)["state"] == "off"  # no AI configured
    AI.save_config(db, "https://api.example.com/v1", "k", "m1", "openai", auto=True); db.commit()
    S.set_value(db, "paused", "true"); db.commit()
    assert AI.review(db)["state"] == "off"  # engine stopped
    S.set_value(db, "paused", "false"); db.commit()
    calls = []
    monkeypatch.setattr(AI, "HTTP", lambda u, h, p: (calls.append(p) or {"choices": [{"message": {"content": "- Sab theek hai\n- Alerts dekho"}}]}))
    r = AI.review(db); db.commit()
    assert r["state"] == "done" and "Sab theek" in S.get(db, "ai_brief")
    assert db.query(AgentEvent).filter(AgentEvent.agent == "advisor").count() == 1
    assert "asha@example.com" not in str(calls[0])
    assert AI.review(db)["state"] == "same" and len(calls) == 1  # nothing changed: no second paid call


def test_models_route_roles_and_button(client, monkeypatch):
    monkeypatch.setattr(AI, "HTTP_GET", lambda u, h: {"data": [{"id": "gpt-x"}, {"id": "whisper-1"}]})
    tok = login(client)
    r = client.post("/assistant/models", json={"base_url": "https://api.example.com/v1", "api_key": "k", "style": "openai"}, headers={"X-CSRF-Token": tok})
    j = r.json()
    assert j["ok"] and [m["id"] for m in j["models"]] == ["gpt-x", "whisper-1"] and j["models"][1]["chat"] is False
    r = client.post("/assistant/models", json={"base_url": "", "api_key": "", "style": "openai"}, headers={"X-CSRF-Token": tok})
    assert r.json()["ok"] is False and r.json()["error"]
    html = client.get("/assistant").text
    assert "Models fetch karo" in html and "Auto mode" in html
    assert "AI model" in client.get("/connect").text
    r = post(client, "/assistant/settings", tok, base_url="https://api.example.com/v1", api_key="k", model="gpt-x, gpt-y", style="openai", auto="on")
    assert r.status_code == 303
    assert "gpt-x, gpt-y" in client.get("/assistant").text


def test_start_turns_on_auto_and_job_registered(db):
    from app.scheduler import SCHEDULE
    from app.services.agents import AGENTS
    assert "ai_review" in SCHEDULE and "advisor" in AGENTS
    EN.start(db); db.commit()
    assert S.get_bool(db, "ai_auto")


# ---- real AI use: strict providers, clear failures --------------------------------------------
import json as _json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Strict(BaseHTTPRequestHandler):
    """Behaves like picky real providers: refuses Python's default User-Agent, 'temperature' and 'max_tokens' (new OpenAI models),
    wants a /v1 path, and wants the chat to start with a user message."""
    seen: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = _json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if "Python-urllib" in self.headers.get("User-Agent", ""):
            return self._send(403, {"error": "blocked agent"})
        self._send(200, {"data": [{"id": "strict-1"}]})

    def do_POST(self):
        body = _json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        _Strict.seen.append(body)
        if "Python-urllib" in self.headers.get("User-Agent", ""):
            return self._send(403, {"error": "blocked agent"})
        if not self.path.startswith("/v1/chat/completions"):
            return self._send(404, {"error": "not found"})
        if self.headers.get("Authorization") != "Bearer sk-live":
            return self._send(401, {"error": {"message": "bad key"}})
        if "temperature" in body:
            return self._send(400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.3 with this model."}})
        if "max_tokens" in body:
            return self._send(400, {"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."}})
        msgs = [m for m in body["messages"] if m["role"] != "system"]
        if msgs[0]["role"] != "user" or any(a["role"] == b["role"] for a, b in zip(msgs, msgs[1:])):
            return self._send(400, {"error": {"message": "messages must alternate starting with user"}})
        self._send(200, {"choices": [{"message": {"content": [{"type": "text", "text": "asli AI jawab"}]}}]})


def _serve():
    srv = HTTPServer(("127.0.0.1", 0), _Strict)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_real_http_against_picky_provider(db):
    AI.SECRET_KEY = "s" * 32; AI._bad.clear(); AI._quirks.clear(); AI.HTTP = None; AI.HTTP_GET = None; _Strict.seen.clear()
    srv = _serve()
    try:
        host = f"http://127.0.0.1:{srv.server_port}"  # note: no /v1 on purpose
        assert [m["id"] for m in AI.list_models(host + "/v1", "sk-live", "openai")] == ["strict-1"]  # agent is not Python-urllib
        assert not AI.save_config(db, host, "sk-live", "strict-1", "openai"); db.commit()
        # chat history starts with an assistant turn and repeats a role: must still be accepted
        hist = [{"role": "assistant", "content": "purana"}, {"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
        r = AI.ask(db, hist, "ab batao")
        assert r["mode"] == "ai" and r["answer"] == "asli AI jawab" and r["model"] == "strict-1" and not r["error"]
        n = len(_Strict.seen)
        assert AI.ask(db, [], "dobara")["mode"] == "ai" and len(_Strict.seen) == n + 1  # quirks are remembered: no more failed attempts
        # wrong key: the user must be told the real reason, not just get the built-in answer
        assert not AI.save_config(db, host, "wrong", "strict-1", "openai"); db.commit()
        r = AI.ask(db, [], "hello")
        assert r["mode"] == "offline" and r["error"] and "401" in r["note"] and "API key" in r["note"]
    finally:
        srv.shutdown()


def test_unreadable_saved_key_is_reported(db):
    AI.SECRET_KEY = "s" * 32
    AI.save_config(db, "https://api.example.com/v1", "k", "m", "openai"); db.commit()
    AI.SECRET_KEY = "different-secret-key-32-characters"  # SECRET_KEY changed after the key was saved
    r = AI.ask(db, [], "hello")
    assert r["mode"] == "offline" and r["error"] and "API key" in r["note"]
    AI.SECRET_KEY = "s" * 32


def test_half_setup_is_reported_and_anthropic_host_detected(db):
    AI.SECRET_KEY = "s" * 32
    S.set_value(db, "ai_base_url", "https://api.openai.com/v1"); db.commit()
    r = AI.ask(db, [], "hello")
    assert r["error"] and "adhura" in r["note"]
    S.set_value(db, "ai_base_url", "https://api.anthropic.com/v1"); S.set_value(db, "ai_style", "openai"); db.commit()
    assert AI.config(db)["style"] == "anthropic"


def test_clean_history():
    h = [{"role": "assistant", "content": "x"}, {"role": "user", "content": "a"}, {"role": "user", "content": "b"}, {"role": "assistant", "content": "c"}]
    assert AI._clean_history(h) == [{"role": "user", "content": "a\nb"}, {"role": "assistant", "content": "c"}]
