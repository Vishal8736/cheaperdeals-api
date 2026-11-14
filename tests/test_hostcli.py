"""Vercel, Render, GitHub and Neon through their CLIs (no API keys). The CLIs are faked; real flags/outputs are unverified until a live run."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from app.models import Setting
from app.version import VERSION
from app.services import bootstrap, gitpush, hostcli, launch, shopcfg
from app.services import settings_service as S
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import login, post

ran: list[dict] = []
W: dict = {}


def fake_run(cmd, cwd=None, timeout=300):
    rec = {"cmd": list(cmd), "cwd": cwd}
    if cwd and (Path(cwd) / "vercel.json").exists():
        rec["vercel_json"] = (Path(cwd) / "vercel.json").read_text()
    ran.append(rec)
    c = " ".join(cmd)
    if c == "vercel whoami":
        return (0, "vishal\n", "") if W.get("vercel", True) else (1, "", "Error: not logged in")
    if c == "render whoami -o text":
        return (0, "Vishal <v@x.com>\n", "") if W.get("render", True) else (1, "", "not logged in")
    if c == "gh auth status":
        return (0, "", "github.com\n  ✓ Logged in to github.com account vishal (keyring)\n") if W.get("github", True) else (1, "", "You are not logged into any GitHub hosts")
    if c == "gh auth token":
        return 0, "ghp_FAKEFROMCLI\n", ""
    if c == "neon me --output json":
        return (0, json.dumps({"email": "v@x.com"}), "") if W.get("neon", True) else (1, "", "not authenticated")
    if c.startswith("neon orgs list"):
        return (0, json.dumps([{"id": "org-fragrant-butterfly-1", "name": "Vishal"}]), "") if W.get("orgs_cmd", True) else (1, "", "unknown command")
    if W.get("neon_needs_org") and c.startswith("neon projects") and "--org-id" not in c:
        return 0, "? What organization would you like to use? » - Use arrow-keys. Return to submit.\n> Vishal (org-fragrant-butterfly-78493929)\n", ""
    if c.startswith("neon projects list --output json"):
        W.setdefault("org_used", []).append(cmd)
        return 0, W["neon_raw"] if "neon_raw" in W else json.dumps(W.get("neon_projects", [])), ""
    if c.startswith("neon projects create"):
        W["neon_created"] = cmd
        return 0, json.dumps({"project": {"id": "p-1", "name": "cheaperdeals"}, "connection_uris": [{"connection_uri": "postgresql://u:NEONPW99@ep-x.neon.tech/neondb?sslmode=require"}]}), ""
    if c.startswith("neon connection-string"):
        return 0, "postgresql://u:EXISTINGPW@ep-y.neon.tech/neondb?sslmode=require\n", ""
    if "render services create" in c:
        W["create"] = cmd
        name = cmd[cmd.index("--name") + 1]
        if W.get("create_fails"):
            return 2, "", "unknown flag: --plan"
        return 0, json.dumps({"id": "srv-9", "name": name, "type": "web_service", "serviceDetails": {"url": f"https://{name}.onrender.com"}}), ""
    if "render services -o json" in c:
        return 0, W["services_raw"] if "services_raw" in W else json.dumps(W.get("services", [])), ""
    if "render deploys create" in c:
        W["deploy_cmd"] = c
        return 0, "deploy started\n", ""
    if "vercel link" in c:
        return 0, "Linked\n", ""
    if "vercel deploy" in c:
        return 0, "Production: https://cheaperdeals-shop-abc.vercel.app\n", ""
    return 1, "", "unknown"


def handler(req: httpx.Request) -> httpx.Response:
    W.setdefault("http", []).append(f"{req.method} {req.url}")
    h, p = req.url.host, req.url.path
    if h == "api.github.com":
        W["gh_auth"] = req.headers.get("authorization")
        if p == "/user":
            return httpx.Response(200, json={"login": "vishal"})
        if p == "/repos/vishal/cheaperdeals-api":
            if req.method == "PATCH":
                W["visibility_patch"] = json.loads(req.content)
                W["repo_private"] = bool(W["visibility_patch"].get("private")) and not W.get("gh_refuses_private")
            return httpx.Response(200, json={"private": W.get("repo_private", False), "name": "cheaperdeals-api"})
        if p.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "c0"}})
        if p.endswith("/git/trees"):
            W["tree"] = json.loads(req.content)
            if W.get("no_workflow_scope") and any(e["path"].startswith(".github/workflows/") for e in W["tree"]["tree"]):
                W["tree_refused"] = W.get("tree_refused", 0) + 1
                return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(201, json={"sha": "x"})
    if p in ("/ping", "/api/ping"):
        return httpx.Response(200, text="ok") if W.get("up", True) else httpx.Response(503, text="down")
    if p == "/health":
        seq = W.get("rver_seq")  # versions the Render server reports on successive calls (simulates a build finishing)
        v = (seq.pop(0) if len(seq) > 1 else seq[0]) if seq else W.get("rver", VERSION)
        return httpx.Response(200, json={"ok": True, **({"version": v} if v else {})})
    if p == "/internal/engine/status":
        if W.get("status_404"):
            return httpx.Response(404)
        return httpx.Response(200, json=W.get("status", {"running": True, "products": 6, "listed": 4, "catalog_error": "", "version": VERSION}))
    if p == "/internal/engine/start":
        W["remote_start"] = {"key": req.headers.get("x-internal-key"), "body": json.loads(req.content)}
        return httpx.Response(200, json={"ok": True})
    return httpx.Response(404)


class FakeProc:
    last = None

    def __init__(self, cmd, **k):
        FakeProc.last = self
        self.cmd = cmd
        self.kw = k
        self.sent = ""
        self.stdin = self
        self.stdout = iter(["\x1b[1mVisit https://vercel.com/oauth/device?user_code=AB-CD to log in\x1b[0m\n", "Waiting for approval...\n"])

    def write(self, t):
        self.sent += t

    def flush(self):
        pass

    def close(self):
        pass

    def wait(self, timeout=None):
        key = {"vercel": "vercel", "render": "render", "gh": "github", "neon": "neon"}.get(Path(self.cmd[0]).name)
        if W.get("login_works") and key:
            W[key] = True  # the person allowed it in the browser
        return 0


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    ran.clear()
    W.clear()
    shopcfg.HTTP = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(hostcli, "RUN", fake_run)
    monkeypatch.setattr(hostcli, "POPEN", FakeProc)
    monkeypatch.setattr(hostcli, "cmd_for", lambda which: {"vercel": ["vercel"], "render": ["render"], "github": ["gh"], "neon": ["neon"]}[which])
    monkeypatch.setattr(hostcli, "_NEON_ORG", [])
    monkeypatch.setattr(launch, "SLEEP", lambda s: None)
    monkeypatch.setattr(launch, "LOGIN_SLEEP", lambda s: __import__("time").sleep(0.01))
    monkeypatch.setattr(gitpush, "SLEEP", lambda s: None)
    monkeypatch.delenv("RENDER", raising=False)
    yield
    shopcfg.HTTP = None


def raw(client, key):
    with client.app.state.database.SessionLocal() as db:
        s = db.get(Setting, key)
        return s.value if s else None


def prep(client, tok, live=False, cj=True):
    if cj:
        with client.app.state.database.SessionLocal() as db:
            bootstrap_key = "CJ1@api@CJKEY123456"
            from app.services import shopcfg as C
            C.save_secret(db, "host_env_cj_enc", bootstrap_key)
            S.set_value(db, "host_use_cli", "true")
            db.commit()
    post(client, "/website/connect/live", tok, live="1" if live else "")


def state(client):
    with client.app.state.database.SessionLocal() as db:
        return launch.state(db), launch.log_of(db), S.get_bool(db, "engine_started_once")


def test_login_commands_and_captured_link(client):
    tok = login(client)
    post(client, "/website/connect/cli/login", tok, which="vercel", next="/keys")
    info = json.loads(raw(client, "cli_login_vercel"))
    assert info["state"] == "done" and info["url"].startswith("https://vercel.com/oauth/device") and "\x1b" not in json.dumps(info)
    assert raw(client, "host_use_cli") == "true"
    post(client, "/website/connect/cli/login", tok, which="github", next="/keys")
    assert FakeProc.last.cmd == ["gh", "auth", "login", "--web", "-h", "github.com", "-p", "https", "-s", "repo,workflow"] and FakeProc.last.sent.startswith("\n")  # Enter for "Press Enter to open github.com"
    post(client, "/website/connect/cli/login", tok, which="neon", next="/keys")
    assert FakeProc.last.cmd == ["neon", "login"]


def test_status_check_for_all_four(client):
    tok = login(client)
    W["neon"] = False
    post(client, "/website/connect/cli/status", tok, next="/keys")
    st = {w: json.loads(raw(client, f"cli_status_{w}")) for w in hostcli.PROVIDERS}
    assert st["vercel"]["who"] == "vishal" and st["github"]["who"] == "vishal" and st["render"]["logged_in"]
    assert st["neon"]["logged_in"] is False


def test_pages_have_no_api_key_or_optional_fields(client):
    login(client)
    for url in ("/keys", "/website/connect"):
        html = client.get(url).text
        for gone in ("Vercel token", "Render API key", "DATABASE_URL", "GitHub token", "Team ID", "optional", "Optional"):
            assert gone not in html, (url, gone)
        for p in ("Vercel", "Render", "GitHub", "Neon"):
            assert f"{p} login (CLI)" in html


def test_start_hosts_everything_with_cli_only(client):
    tok = login(client)
    prep(client, tok)
    post(client, "/engine/start", tok, next="/")
    st, log, started = state(client)
    assert st == "ok" and started, log
    create = W["create"]
    assert create[create.index("--plan") + 1] == "free" and create[create.index("--repo") + 1] == "https://github.com/vishal/cheaperdeals-api"
    envs = {c.split("=", 1)[0]: c.split("=", 1)[1] for c in create if "=" in c and c.split("=")[0].isupper()}
    assert envs["CJ_API_KEY"].endswith("CJKEY123456") and envs["DATABASE_URL"].startswith("postgresql://u:NEONPW99") and len(envs["ADMIN_PASSWORD"]) >= 12
    assert envs["DRY_RUN"] == "true"
    assert "--proxy-headers" in create[create.index("--start-command") + 1]  # behind Render's proxy: real client IPs for the login limiter
    assert any("neon projects create" in " ".join(r["cmd"]) for r in ran)
    assert W["gh_auth"] == "Bearer ghp_FAKEFROMCLI" and any(e["path"] == "render.yaml" for e in W["tree"]["tree"])
    assert not any(e["path"] in ("local.env", ".env") for e in W["tree"]["tree"])
    dep = next(r for r in ran if "vercel_json" in r and "deploy" in r["cmd"])
    assert "https://cheaperdeals-api.onrender.com" in dep["vercel_json"]
    assert raw(client, "shop_site_url") == "https://cheaperdeals-shop.vercel.app"
    assert W["remote_start"]["body"]["live"] is False and W["remote_start"]["key"]
    assert not any("api.vercel.com" in h or "api.render.com" in h for h in W["http"])


def test_db_address_and_admin_password_are_sealed_and_reused(client):
    tok = login(client)
    prep(client, tok)
    post(client, "/engine/start", tok, next="/")
    for k in ("host_env_db_enc", "host_env_admin_enc", "host_env_cj_enc", "host_internal_key_enc"):
        v = raw(client, k)
        assert v and "NEONPW99" not in v and "CJKEY123456" not in v
    client.get("/website/connect")  # show (and clear) the earlier messages, like a browser does after each redirect
    post(client, "/website/connect/admin-password", tok)
    page = client.get("/website/connect").text
    with client.app.state.database.SessionLocal() as db:
        from app.services import shopcfg as C
        pw = C.read_secret(db, "host_env_admin_enc")
    assert pw and pw in page  # the owner can read the generated Render admin password once asked


def test_existing_backend_and_site_are_reused_nothing_created(client):
    tok = login(client)
    prep(client, tok)
    W["services"] = [{"id": "srv-1", "name": "cheaperdeals-api", "type": "web_service", "url": "cheaperdeals-api.onrender.com"}]
    post(client, "/engine/start", tok, next="/")  # backend reused, website deployed
    ran.clear()
    W.pop("create", None)
    post(client, "/engine/start", tok, next="/")  # both exist and answer
    st, log, _ = state(client)
    assert st == "ok", log
    assert "create" not in W and not [r for r in ran if "deploy" in r["cmd"] or "link" in r["cmd"]]
    assert not any(h.startswith("POST https://api.github.com") for h in W["http"])
    assert any("pehle se maujood" in m for _, m in log)


def test_reuse_path_needs_no_github_or_neon_login(client):
    tok = login(client)
    prep(client, tok)
    W["services"] = [{"id": "srv-1", "name": "cheaperdeals-api", "type": "web_service", "url": "https://cheaperdeals-api.onrender.com"}]
    W["github"] = W["neon"] = False
    post(client, "/engine/start", tok, next="/")
    assert state(client)[0] == "ok"


def test_missing_logins_stop_before_anything_is_created(client, monkeypatch):
    tok = login(client)
    prep(client, tok)
    W["render"] = False
    post(client, "/engine/start", tok, next="/")
    st, log, started = state(client)
    assert st == "error" and not started and any("Render (backend) login poora nahi hua" in m for _, m in log)
    W["render"] = True
    W["neon"] = False
    post(client, "/engine/start", tok, next="/")
    assert any("Neon (database) login poora nahi hua" in m for _, m in state(client)[1]) and "create" not in W
    W["neon"] = True
    W["github"] = False
    post(client, "/engine/start", tok, next="/")
    assert any("GitHub (code) login poora nahi hua" in m for _, m in state(client)[1]) and "create" not in W
    monkeypatch.setattr(hostcli, "cmd_for", lambda which: None)
    post(client, "/engine/start", tok, next="/")
    assert any("CLI is computer par nahi mili" in m for _, m in state(client)[1])


def test_cj_key_missing_is_reported(client):
    tok = login(client)
    prep(client, tok, cj=False)
    with client.app.state.database.SessionLocal() as db:
        S.set_value(db, "host_use_cli", "true")
        db.commit()
    post(client, "/engine/start", tok, next="/")
    assert any("CJ API key" in m for _, m in state(client)[1]) and "create" not in W


def test_render_failure_shows_message_with_blueprint_fallback(client):
    tok = login(client)
    prep(client, tok)
    W["create_fails"] = True
    post(client, "/engine/start", tok, next="/")
    st, log, started = state(client)
    assert st == "error" and not started and any("blueprint/new" in m for _, m in log)


def test_backend_that_never_answers_is_reported(client):
    tok = login(client)
    prep(client, tok)
    W["up"] = False
    post(client, "/engine/start", tok, next="/")
    st, log, started = state(client)
    assert st == "error" and not started and any("ping" in m for _, m in log)


def test_live_choice_flows_to_render_and_remote_start(client):
    tok = login(client)
    prep(client, tok, live=True)
    post(client, "/engine/start", tok, next="/")
    envs = [c for c in W["create"] if c.startswith("DRY_RUN=")]
    assert envs == ["DRY_RUN=false"] and W["remote_start"]["body"]["live"] is True


def test_local_only_start_and_plain_start(client):
    tok = login(client)
    prep(client, tok)
    ran.clear()
    post(client, "/engine/start", tok, next="/", local="1")
    assert not ran and state(client)[2] is True


def test_secrets_never_in_launch_log(client):
    tok = login(client)
    prep(client, tok)
    W["create_fails"] = True
    orig = fake_run

    def leaky(cmd, cwd=None, timeout=300):
        if "create" in cmd and "render" in cmd[0]:
            return 2, "", "bad value CJ1@api@CJKEY123456 and postgresql://u:NEONPW99@ep-x.neon.tech"
        return orig(cmd, cwd, timeout)
    hostcli.RUN = leaky
    post(client, "/engine/start", tok, next="/")
    log = json.dumps(state(client)[1])
    assert "CJKEY123456" not in log and "NEONPW99" not in log


def test_cj_key_from_the_page_is_what_the_launcher_sends(client, monkeypatch):
    tok = login(client)
    monkeypatch.setattr("app.connectors.manager.ConnectorManager.test", lambda self, db, conn: (True, "ok"))
    post(client, "/keys/cj", tok, api_key="CJ1@api@FROMPAGE777")
    from app.services import shopcfg as C
    with client.app.state.database.SessionLocal() as db:
        assert C.read_secret(db, "host_env_cj_enc") == "CJ1@api@FROMPAGE777"


def test_remote_internal_start_endpoint(client):
    key = client.app.state.cfg.internal_api_key
    body = {"live": True, "site_url": "https://shop.vercel.app"}
    assert client.post("/internal/engine/start", json=body).status_code == 401
    r = client.post("/internal/engine/start", json=body, headers={"X-Internal-Key": key})
    assert r.status_code == 200 and r.json()["running"] and r.json()["dry_run"] is False
    assert raw(client, "default_logistic_name") == "AUTO" and raw(client, "shop_site_url") == "https://shop.vercel.app"


def test_wizard_counts_our_own_store(client):
    tok = login(client)
    prep(client, tok)
    post(client, "/engine/start", tok, next="/")
    with client.app.state.database.SessionLocal() as db:
        from app.services import wizard
        d = {s["key"]: s for s in wizard.steps(db, client.app.state.cfg)}
    assert d["hosting"]["done"] is True and d["store"]["required"] is False and "Dukaan online karo" in client.get("/setup").text


def test_github_push_skips_secret_files():
    names = {p.name for p in gitpush.project_files()}
    assert "local.env" not in names and ".env" not in names and "render.yaml" in names
    assert not any(n.endswith((".db", ".zip")) for n in names)


def test_windows_cmd_shims_are_launched_by_full_path(monkeypatch):
    monkeypatch.undo()
    paths = {"vercel": None, "npx": r"C:\Program Files\nodejs\npx.CMD", "render": r"C:\tools\render.exe", "gh": r"C:\gh\gh.exe", "neon": None, "neonctl": None}
    monkeypatch.setattr(hostcli.shutil, "which", lambda n: paths.get(n))
    assert hostcli.cmd_for("vercel") == [r"C:\Program Files\nodejs\npx.CMD", "--yes", "vercel@latest"]
    assert hostcli.cmd_for("neon") == [r"C:\Program Files\nodejs\npx.CMD", "--yes", "neonctl@7.0.8"]
    assert hostcli.cmd_for("github") == [r"C:\gh\gh.exe"] and hostcli.cmd_for("render") == [r"C:\tools\render.exe"]
    paths["neonctl"] = r"C:\npm\neonctl.CMD"
    assert hostcli.cmd_for("neon") == [r"C:\npm\neonctl.CMD"]


def test_launch_error_is_shown_not_swallowed(client, monkeypatch):
    tok = login(client)

    def boom(*a, **k):
        raise FileNotFoundError(2, "The system cannot find the file specified")
    monkeypatch.setattr(hostcli, "POPEN", boom)
    post(client, "/website/connect/cli/login", tok, which="vercel", next="/keys")
    info = json.loads(raw(client, "cli_login_vercel"))
    assert info["state"] == "failed" and any("Node.js" in l for l in info["lines"])
    monkeypatch.setattr(hostcli, "RUN", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("x")))
    assert hostcli.status("vercel")["installed"] is False


def test_neon_npx_skips_a_version_that_cannot_install_and_remembers_the_pick(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(hostcli.shutil, "which", lambda n: {"npx": "/usr/bin/npx"}.get(n))
    monkeypatch.setattr(hostcli, "_NEON_PICK", [])
    tried = []

    def fake(cmd, cwd=None, timeout=300):
        tried.append(cmd[2])
        return (1, "", "npm error code ETARGET") if cmd[2] == "neonctl@7.0.8" else (0, "2.29.3", "")
    monkeypatch.setattr(hostcli, "RUN", fake)
    assert hostcli.cmd_for("neon") == ["/usr/bin/npx", "--yes", "neonctl@2.29.3"]
    assert hostcli.cmd_for("neon") == ["/usr/bin/npx", "--yes", "neonctl@2.29.3"]
    assert tried == ["neonctl@7.0.8", "neonctl@2.29.3"]  # second call used the remembered pick, no re-probe


def test_cli_buttons_keep_the_scroll_position(client):
    tok = login(client)
    html = client.get("/keys").text
    assert 'id="cli-section"' in html and "scrollRestoration" in html and "sessionStorage" in html
    assert "cli-scroll:" in client.get("/website/connect").text


def test_render_no_workspace_is_fixed_automatically_when_there_is_only_one(monkeypatch, tmp_path):
    cfg = tmp_path / "cli.yaml"
    cfg.write_text("version: 1\nworkspace: old\nworkspace_name: old\napi:\n  key: SECRETKEEP\n", encoding="utf-8")
    monkeypatch.setenv("RENDER_CLI_CONFIG_PATH", str(cfg))
    monkeypatch.delenv("RENDER_WORKSPACE", raising=False)
    monkeypatch.setattr(hostcli, "cmd_for", lambda which: ["render"])
    calls = []

    def fake(cmd, cwd=None, timeout=300):
        c = " ".join(cmd)
        calls.append(c)
        if c.startswith("render workspaces"):
            return 0, json.dumps([{"id": "tea-123", "name": "Vishal's workspace"}]), ""
        if c.startswith("render services"):
            if "tea-123" in cfg.read_text(encoding="utf-8") and os.environ.get("RENDER_WORKSPACE") == "tea-123":
                return 0, "[]", ""
            return 1, "", "Error: no workspace set. Use `render workspace set` to set a workspace"
        return 1, "", "unknown"
    monkeypatch.setattr(hostcli, "RUN", fake)
    assert hostcli.render_services() == []
    text = cfg.read_text(encoding="utf-8")
    assert "workspace: tea-123" in text and "workspace_name:" in text and "SECRETKEEP" in text and "version: 1" in text and "old" not in text
    assert text.count("workspace:") == 1 and "\n  id:" not in text  # FLAT keys, never a nested map
    assert sum(c.startswith("render services") for c in calls) == 2


def test_nested_workspace_written_by_v14_2_is_repaired_so_login_works_again(monkeypatch, tmp_path):
    cfg = tmp_path / "cli.yaml"
    cfg.write_text("version: 1\napi:\n  key: KEEPME\nworkspace:\n  id: tea-9\n  name: \"My ws\"\nhost: x\n", encoding="utf-8")
    monkeypatch.setenv("RENDER_CLI_CONFIG_PATH", str(cfg))
    assert hostcli.repair_render_cfg() is True
    t = cfg.read_text(encoding="utf-8")
    assert "workspace: tea-9" in t and 'workspace_name: "My ws"' in t and "KEEPME" in t and "host: x" in t and "\n  id:" not in t
    assert hostcli.repair_render_cfg() is False  # nothing left to repair


def test_render_status_and_login_repair_the_config_first(monkeypatch, tmp_path):
    cfg = tmp_path / "cli.yaml"
    cfg.write_text("workspace:\n  id: tea-1\n  name: n\n", encoding="utf-8")
    monkeypatch.setenv("RENDER_CLI_CONFIG_PATH", str(cfg))
    monkeypatch.setattr(hostcli, "cmd_for", lambda which: ["render"])
    monkeypatch.setattr(hostcli, "RUN", lambda cmd, cwd=None, timeout=300: (0, "ID: usr-1", ""))
    assert hostcli.status("render")["logged_in"] is True
    assert "workspace: tea-1" in cfg.read_text(encoding="utf-8")


def test_render_no_workspace_with_many_asks_for_one_terminal_command(monkeypatch, tmp_path):
    monkeypatch.setenv("RENDER_CLI_CONFIG_PATH", str(tmp_path / "cli.yaml"))
    monkeypatch.setattr(hostcli, "cmd_for", lambda which: ["render"])
    monkeypatch.setattr(hostcli, "RUN", lambda cmd, cwd=None, timeout=300: (0, json.dumps([{"id": "a", "name": "One"}, {"id": "b", "name": "Two"}]), "") if cmd[1] == "workspaces" else (1, "", "Error: no workspace set"))
    with pytest.raises(hostcli.CliError) as e:
        hostcli.render_services()
    assert "render workspace set" in str(e.value) and "One" in str(e.value) and "Two" in str(e.value)
    assert not (tmp_path / "cli.yaml").exists()


def test_vercel_site_prefers_the_aliased_line_and_never_the_one_off_deployment_url():
    out = "https://shop-abc123-me.vercel.app\nProduction: https://shop-abc123-me.vercel.app [3s]\nAliased: https://shop-me.vercel.app [3s]\n"
    assert hostcli.vercel_site(out, "shop") == "https://shop-me.vercel.app"
    assert hostcli.vercel_site("Production: https://shop-abc123-me.vercel.app [3s]", "shop") == "https://shop.vercel.app"


def test_second_login_click_is_ignored_and_a_dead_login_does_not_stay_running_for_ever(client):
    Session = client.app.state.database.SessionLocal
    hostcli._LOGGING_IN.discard("vercel")
    db = Session()
    hostcli._store_login(Session, "vercel", ["waiting"], "running")  # a login the previous app process started, then the app restarted
    info = hostcli.login_info(db, "vercel")
    assert info["state"] == "failed" and "dobara dabao" in info["lines"][-1]
    hostcli._LOGGING_IN.add("vercel")  # a login thread that really is alive
    assert hostcli.login_info(db, "vercel")["state"] == "running"
    spawned = []
    import threading as T
    orig = T.Thread
    T.Thread = lambda *a, **k: spawned.append(1) or type("X", (), {"start": lambda self: None})()
    try:
        hostcli.start_login(Session, "vercel", background=True)
    finally:
        T.Thread = orig
        hostcli._LOGGING_IN.discard("vercel")
        db.close()
    assert spawned == []  # no second browser login while one is waiting


def test_neon_npx_failure_is_remembered_so_it_does_not_probe_again_and_again(monkeypatch):
    monkeypatch.undo()  # the autouse fixture fakes cmd_for; this test needs the real one
    monkeypatch.setattr(hostcli.shutil, "which", lambda n: {"npx": "/usr/bin/npx"}.get(n))
    monkeypatch.setattr(hostcli, "_NEON_PICK", [])
    n = []
    monkeypatch.setattr(hostcli, "RUN", lambda cmd, cwd=None, timeout=300: (n.append(1) or (1, "", "ETARGET")))
    hostcli.cmd_for("neon")
    hostcli.cmd_for("neon")
    assert len(n) == len(hostcli.NEON_VERSIONS)


def test_start_warns_when_render_runs_older_code(client):
    tok = login(client)
    prep(client, tok)
    W["services"] = [{"id": "srv-1", "name": "cheaperdeals-api", "type": "web_service", "serviceDetails": {"url": "https://cheaperdeals-api.onrender.com"}}]
    W["rver"] = "15.1"
    post(client, "/website/connect/launch", tok)
    st, lg, _ = state(client)
    assert any("v15.1" in m and "naya code" in m for _, m in lg) and raw(client, "host_remote_version") == "15.1"
    assert "Render par v15.1" in client.get("/website/connect").text


def test_preflight_changes_nothing_and_says_what_is_missing(client):
    tok = login(client)
    prep(client, tok, cj=False)
    W["render"] = False
    W["services"] = []
    before = len(ran)
    post(client, "/website/connect/preflight", tok)
    pre = json.loads(raw(client, "host_preflight"))["checks"]
    by = {i["name"]: i for i in pre}
    assert by["CJ API key"]["ok"] is False and by["Render (backend)"]["ok"] is False and "login (CLI)" in by["Render (backend)"]["fix"]
    assert any(k.startswith("Detection: ") for k in by)
    assert by["Vercel (frontend)"]["ok"] is True
    assert not any(any(w in " ".join(r["cmd"]) for w in ("create", "deploy", "link")) for r in ran[before:])  # looked, did not build anything
    html = client.get("/website/connect").text
    assert "Start se pehle jaanch" in html and "ruka" in html


def test_preflight_all_ready_when_backend_exists(client):
    tok = login(client)
    prep(client, tok)
    W["services"] = [{"id": "srv-1", "name": "cheaperdeals-api", "type": "web_service", "serviceDetails": {"url": "https://cheaperdeals-api.onrender.com"}}]
    with client.app.state.database.SessionLocal() as db:
        S.set_value(db, "host_render_service", "srv-1")
        db.commit()
    post(client, "/website/connect/preflight", tok)
    by = {i["name"]: i for i in json.loads(raw(client, "host_preflight"))["checks"]}
    assert by["Detection: Render (backend)"]["ok"] is True and by["Render ka code version"]["ok"] is True
    assert by["GitHub (code)"]["ok"] is True and by["Neon (database)"]["ok"] is True


def _hosted(client, tok):
    prep(client, tok)
    with client.app.state.database.SessionLocal() as db:
        for k, v in (("host_repo", "https://github.com/vishal/cheaperdeals-api"), ("host_render_url", "https://cheaperdeals-api.onrender.com"), ("host_render_service", "srv-1")):
            S.set_value(db, k, v)
        db.commit()


def test_update_code_pushes_to_github_and_waits_for_render_to_report_the_new_version(client):
    tok = login(client)
    _hosted(client, tok)
    W["rver_seq"] = ["15.1", "15.1", VERSION]
    post(client, "/website/connect/update", tok)
    st, lg, _ = state(client)
    assert st == "ok" and any(f"v{VERSION}" in m and lv == "ok" for lv, m in lg)
    assert W["gh_auth"] == "Bearer ghp_FAKEFROMCLI" and any(e["path"] == "render.yaml" for e in W["tree"]["tree"])
    assert "deploy_cmd" not in W  # auto-deploy was enough
    assert raw(client, "host_remote_version") == VERSION


def test_update_code_asks_render_to_build_when_auto_deploy_does_not_happen(client):
    tok = login(client)
    _hosted(client, tok)
    W["rver_seq"] = ["15.1"] * 19 + [VERSION]  # stays old for the whole auto-deploy wait, then the manual deploy works
    post(client, "/website/connect/update", tok)
    st, lg, _ = state(client)
    assert st == "ok" and W["deploy_cmd"].startswith("render deploys create srv-1")


def test_update_code_before_any_hosting_explains_itself(client):
    tok = login(client)
    prep(client, tok)
    post(client, "/website/connect/update", tok)
    st, lg, _ = state(client)
    assert st == "error" and any("Pehle Start" in m for _, m in lg)


def test_database_urls_from_hosts_are_normalised():
    from app.db import normalise_url
    assert normalise_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_url("postgresql://u:p@ep-cool-123-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require") == "postgresql+psycopg://u:p@ep-cool-123.us-east-2.aws.neon.tech/neondb?sslmode=require"
    assert normalise_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_url("sqlite:///x.db") == "sqlite:///x.db"


def test_public_url_falls_back_to_the_address_render_provides(monkeypatch):
    from app.config import Config
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://abc.onrender.com")
    assert Config().public_url == "https://abc.onrender.com"
    monkeypatch.setenv("PUBLIC_URL", "https://shop.example.com")
    assert Config().public_url == "https://shop.example.com"


def test_health_reports_the_version(client):
    assert client.get("/health").json() == {"ok": True, "version": VERSION}


def test_non_ascii_csrf_or_internal_key_is_denied_not_a_crash(client):
    tok = login(client)
    r = client.post("/website/connect/live", data={"live": "1"}, headers={"x-csrf-token": "t\u00e9st\u20ac".encode("utf-8")})
    assert r.status_code == 403
    r = client.post("/internal/engine/start", json={}, headers={"x-internal-key": "k\u00e9y\u20ac".encode("utf-8")})
    assert r.status_code == 401


def test_start_logs_in_by_itself_when_a_cli_is_not_logged_in(client):
    tok = login(client)
    prep(client, tok)
    W["render"] = False
    W["login_works"] = True  # the person allows it in the browser while Start waits
    post(client, "/engine/start", tok, next="/")
    st, lg, started = state(client)
    assert st == "ok" and started
    assert any("Render (backend) mein login nahi" in m and lv == "warn" for lv, m in lg) and any("Render (backend) login ho gaya" in m for _, m in lg)
    assert any("Browser na khule to ye link kholo" in m for _, m in lg)


def test_render_account_with_no_services_prints_null_and_start_builds_everything(client):
    """The crash from the screenshot: `render services -o json` printed `null`, Start died with 'NoneType' has no attribute 'get'."""
    tok = login(client)
    prep(client, tok)
    for raw_out in ("null", "[]", "", "{}"):
        W.clear()
        W["services_raw"] = raw_out
        post(client, "/engine/start", tok, next="/")
        st, lg, started = state(client)
        assert st == "ok" and started and W["create"][W["create"].index("--name") + 1] == "cheaperdeals-api", raw_out
        with client.app.state.database.SessionLocal() as db:  # next round starts from scratch
            for k in ("host_render_service", "host_render_url", "host_backend_url", "host_repo", "host_vercel_url"):
                S.set_value(db, k, "")
            db.commit()


def test_start_detects_what_exists_logs_a_plan_and_saves_it(client):
    tok = login(client)
    prep(client, tok)
    W["services"] = []
    post(client, "/engine/start", tok, next="/")
    st, lg, _ = state(client)
    det = {i["key"]: i for i in json.loads(raw(client, "host_detect"))["items"]}
    assert det["render"]["state"] == "missing" and det["neon"]["state"] in ("missing", "unknown") and det["vercel"]["state"] in ("found", "missing")
    assert any(m.startswith("Detection:") and "Render (backend): nahi mila" in m for _, m in lg)
    assert any("Jo nahi mila wo ab khud banaunga" in m and "Render (backend)" in m for _, m in lg)
    page = client.get("/website/connect").text
    assert "Detection" in page and "nahi mila" in page


def test_existing_service_is_found_by_its_github_repo_even_under_another_name(client):
    tok = login(client)
    prep(client, tok)
    with client.app.state.database.SessionLocal() as db:
        S.set_value(db, "host_repo", "https://github.com/vishal/my-shop-backend")
        db.commit()
    W["services"] = [{"id": "srv-77", "name": "something-else", "type": "web_service", "repo": "https://github.com/vishal/my-shop-backend", "serviceDetails": {"url": "https://something-else.onrender.com"}}]
    post(client, "/engine/start", tok, next="/")
    st, lg, _ = state(client)
    assert st == "ok" and "create" not in W and raw(client, "host_render_service") == "srv-77"


def test_json_items_treats_every_empty_answer_as_an_empty_list():
    for out in ("null", "[]", "{}", "", "  ", "No services found", '{"services": null}', "42", '"x"'):
        assert hostcli.json_items(out, ("services",), "Render", "render services") == [], out
    assert hostcli.json_items('{"services":[{"id":1}]}', ("services",), "R", "c") == [{"id": 1}]
    with pytest.raises(hostcli.CliError):
        hostcli.json_items("<html>oops", ("services",), "Render", "render services")


def test_json_items_survives_banners_and_tables():
    keys = ("projects", "shared")
    banner = "npm warn exec The following package was not found and will be installed: neonctl@7.0.8\n"
    assert hostcli.json_items(banner + '[{"id": "p1", "name": "a"}]\nUpdate available 9.9', keys, "Neon", "c") == [{"id": "p1", "name": "a"}]
    assert hostcli.json_items('{"projects": [{"id": "p1"}], "shared": []}', keys, "Neon", "c") == [{"id": "p1"}]
    table = "Projects\n┌────┬──────────────┐\n│ Id │ Name         │\n├────┼──────────────┤\n│ p1 │ cheaperdeals │\n└────┴──────────────┘"
    assert hostcli.json_items(table, keys, "Neon", "c") == [{"id": "p1", "name": "cheaperdeals"}]
    assert hostcli.json_items("Id    Name\np1    cheaperdeals", keys, "Neon", "c") == [{"id": "p1", "name": "cheaperdeals"}]
    with pytest.raises(hostcli.CliError) as e:
        hostcli.json_items("WARN something odd", keys, "Neon", "c")
    assert "WARN something odd" in str(e.value)  # the error now shows what the CLI really printed


def test_start_works_when_neon_prints_banner_or_table_instead_of_clean_json(client):
    tok = login(client)
    for raw_out in ('npm warn exec stuff\n[{"id": "p-9", "name": "cheaperdeals"}]', "Id  Name\np-9  cheaperdeals", "WARNING: new neonctl\n[]"):
        W.clear()
        prep(client, tok)
        W["neon_raw"] = raw_out
        post(client, "/engine/start", tok, next="/")
        st, lg, started = state(client)
        assert st == "ok" and started, (raw_out, lg[-3:])
        with client.app.state.database.SessionLocal() as db:
            for k in ("host_render_service", "host_render_url", "host_backend_url", "host_repo", "host_vercel_url", "host_env_db_enc"):
                S.set_value(db, k, "")
            db.commit()


def test_neon_organization_prompt_is_answered_with_org_id(client):
    """The screenshot: neonctl printed 'What organization would you like to use?' and waited, so Start stopped at Neon."""
    tok = login(client)
    prep(client, tok)
    W["neon_needs_org"] = True
    post(client, "/engine/start", tok, next="/")
    st, lg, started = state(client)
    assert st == "ok" and started, lg[-3:]
    assert any("--org-id" in r for r in W["org_used"]) and W["org_used"][-1][-1] == "org-fragrant-butterfly-78493929"
    assert "--org-id" in W["neon_created"] and W["neon_created"][-1] == "org-fragrant-butterfly-78493929"


def test_neon_org_falls_back_to_orgs_list_when_prompt_shows_no_id(client):
    c = ["neon"]
    W.clear()
    W["neon_needs_org"] = True
    assert hostcli._neon_org(c, "? What organization would you like to use?") == "org-fragrant-butterfly-1"
    W["orgs_cmd"] = False
    assert hostcli._neon_org(c, "? What organization would you like to use?") == ""
    seen = []
    def prompt_only(cmd, **kw):
        seen.append(cmd)
        return "? What organization would you like to use? » - Use arrow-keys."
    orig = hostcli._exec
    hostcli._exec = lambda cmd, **kw: prompt_only(cmd) if cmd[1:2] != ["orgs"] else orig(cmd, **kw)
    try:
        with pytest.raises(hostcli.CliError) as e:
            hostcli._neon_run(c, ["projects", "list", "--output", "json"])
    finally:
        hostcli._exec = orig
    assert "org-id" in str(e.value)


def test_github_token_without_workflow_scope_still_uploads_everything_else(client):
    """The screenshot: 'GitHub (files upload) ne mana kiya (404). Not Found' although gh was logged in. Cause: .github/workflows/keepalive.yml needs the token's
    `workflow` scope, and GitHub then refuses the whole tree."""
    tok = login(client)
    prep(client, tok)
    W["no_workflow_scope"] = True
    post(client, "/engine/start", tok, next="/")
    st, lg, started = state(client)
    assert st == "ok" and started, lg[-3:]
    assert W["tree_refused"] == 1 and not any(e["path"].startswith(".github/") for e in W["tree"]["tree"]) and len(W["tree"]["tree"]) > 50
    assert any("workflow" in m and "keepalive.yml" in m and lv == "warn" for lv, m in lg)
    assert any(r["cmd"][-1] == "repo,workflow" for r in ran if r["cmd"][:2] == ["gh", "auth"]) or "repo,workflow" in " ".join(hostcli.LOGIN_ARGS["github"])


def test_github_other_failures_are_not_swallowed(client):
    tok = login(client)
    prep(client, tok)
    with pytest.raises(gitpush.GitError) as e:
        orig = gitpush._tree
        gitpush._tree = lambda *a, **k: (_ for _ in ()).throw(gitpush.GitError("x", 401))
        try:
            gitpush.publish("t", "cheaperdeals-api")
        finally:
            gitpush._tree = orig
    assert e.value.status == 401


def test_start_watches_that_products_really_arrive_on_render(client):
    tok = login(client)
    prep(client, tok)
    post(client, "/engine/start", tok, next="/")
    st, lg, started = state(client)
    assert st == "ok" and any("Render par 4 products website par lag gaye" in m for _, m in lg)
    assert any("Catalog agent CJ se products la raha hai" in m for _, m in lg)


def test_start_says_plainly_when_no_product_arrives_and_still_finishes(client):
    tok = login(client)
    prep(client, tok)
    W["status"] = {"running": True, "products": 0, "listed": 0, "catalog_error": "Auto catalog ruk gaya: CJ API key galat", "version": VERSION}
    post(client, "/engine/start", tok, next="/")
    st, lg, started = state(client)
    assert st == "ok" and started
    assert any("Abhi tak koi product website par nahi laga" in m and "CJ API key galat" in m and lv == "warn" for lv, m in lg)


def test_start_with_old_render_version_without_status_endpoint_only_warns(client):
    tok = login(client)
    prep(client, tok)
    W["status_404"] = True
    post(client, "/engine/start", tok, next="/")
    st, lg, started = state(client)
    assert st == "ok" and any("ginti nahi padh paya" in m for _, m in lg)


def test_repo_private_button_makes_the_repo_private_and_verifies_it(client):
    tok = login(client)
    prep(client, tok)
    with client.app.state.database.SessionLocal() as db:
        S.set_value(db, "host_repo", "https://github.com/vishal/cheaperdeals-api")
        db.commit()
    assert "GitHub repo private karo" in client.get("/website/connect").text
    r = post(client, "/website/connect/private", tok)
    assert W["visibility_patch"] == {"private": True}
    assert raw(client, "host_repo_private") == "true"
    assert "ab private hai" in client.get("/website/connect").text  # flash, with the Render access warning
    W["repo_private"] = True
    W.pop("visibility_patch")
    post(client, "/website/connect/private", tok)
    assert "visibility_patch" not in W  # already private: nothing changed


def test_repo_private_is_not_claimed_when_github_does_not_show_it(client):
    tok = login(client)
    prep(client, tok)
    with client.app.state.database.SessionLocal() as db:
        S.set_value(db, "host_repo", "https://github.com/vishal/cheaperdeals-api")
        db.commit()
    W["gh_refuses_private"] = True
    client.get("/website/connect")  # shows (and so clears) the earlier flash from prep()
    post(client, "/website/connect/private", tok)
    assert raw(client, "host_repo_private") != "true"
    assert "private nahi dikhaya" in client.get("/website/connect").text


def test_new_repo_is_created_private_after_the_choice(client):
    tok = login(client)
    prep(client, tok)
    with client.app.state.database.SessionLocal() as db:
        S.set_value(db, "host_repo_private", "true")
        db.commit()
    seen = []
    orig = gitpush._call
    gitpush._call = lambda t, m, p, **kw: (seen.append((m, p, kw.get("json"))) or orig(t, m, p, **kw))
    try:
        post(client, "/engine/start", tok, next="/")
    finally:
        gitpush._call = orig
    assert any(m == "POST" and p == "/user/repos" and j["private"] is True for m, p, j in seen)


# ---- v18: Start brings Render and Vercel up to date by itself ---------------------------------------------------
def _start_twice_setup(client, tok):
    prep(client, tok)
    W["services"] = [{"id": "srv-1", "name": "cheaperdeals-api", "type": "web_service", "url": "https://cheaperdeals-api.onrender.com"}]
    post(client, "/engine/start", tok, next="/")  # first Start: reuses the backend, deploys the website, remembers what it sent
    assert state(client)[0] == "ok"
    with client.app.state.database.SessionLocal() as db:
        S.set_value(db, "host_repo", "https://github.com/vishal/cheaperdeals-api")  # the repo Start created on the very first run
        db.commit()
    ran.clear()
    W.pop("tree", None)


def test_start_sends_new_code_to_render_when_the_server_is_behind(client):
    tok = login(client)
    _start_twice_setup(client, tok)
    W["rver_seq"] = ["15.1"] * 3 + [VERSION]
    post(client, "/engine/start", tok, next="/")
    st, log, _ = state(client)
    assert st == "ok", log
    assert any(e["path"] == "render.yaml" for e in W["tree"]["tree"]) and any("Render par ab v" in m for _, m in log)
    assert any("purana hai" in m for _, m in log)


def test_start_pushes_when_code_changed_even_with_same_version(client):
    tok = login(client)
    _start_twice_setup(client, tok)
    with client.app.state.database.SessionLocal() as db:
        S.set_value(db, "host_pushed_hash", "an-older-fingerprint")
        db.commit()
    post(client, "/engine/start", tok, next="/")
    st, log, _ = state(client)
    assert st == "ok" and "tree" in W and any("naya build" in m for _, m in log)
    post(client, "/engine/start", tok, next="/")  # nothing changed since: nothing is sent again
    W.pop("tree", None)
    post(client, "/engine/start", tok, next="/")
    assert "tree" not in W and any("already naya" in m for _, m in state(client)[1])


def test_start_carries_on_when_the_update_cannot_be_sent(client):
    tok = login(client)
    _start_twice_setup(client, tok)
    W["rver_seq"] = ["15.1"]
    W["github"] = False
    post(client, "/engine/start", tok, next="/")
    st, log, started = state(client)
    assert st == "ok" and started and any(lv == "warn" and "update nahi ho paya" in m for lv, m in log)
    assert "tree" not in W


def test_start_updates_the_website_when_its_design_changed(client):
    tok = login(client)
    _start_twice_setup(client, tok)
    with client.app.state.database.SessionLocal() as db:
        S.set_value(db, "host_site_hash", "old-site")
        db.commit()
    post(client, "/engine/start", tok, next="/")
    assert state(client)[0] == "ok" and any("deploy" in r["cmd"] for r in ran)
    ran.clear()
    post(client, "/engine/start", tok, next="/")  # same design now: no second deploy
    assert not [r for r in ran if "deploy" in r["cmd"]]


def test_auto_update_can_be_switched_off(client):
    tok = login(client)
    _start_twice_setup(client, tok)
    with client.app.state.database.SessionLocal() as db:
        S.set_value(db, "auto_update", "false")
        S.set_value(db, "host_site_hash", "old-site")
        db.commit()
    W["rver_seq"] = ["15.1"]
    post(client, "/engine/start", tok, next="/")
    assert "tree" not in W and not [r for r in ran if "deploy" in r["cmd"]]
