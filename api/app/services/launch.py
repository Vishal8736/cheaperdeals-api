"""Start button, part 1: make sure the store's backend (Render) and website (Vercel) exist and answer; host whatever is missing; then wake the engine.

Everything goes through the providers' own CLIs (you log in once in the browser): Vercel, Render, GitHub (code) and Neon (free database).
Each step checks the REAL state (CLI listing + a live /ping) and logs what it found. Nothing is assumed."""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time

import httpx

from . import diag, engine, gitpush, hostcli, hosting
from ..version import VERSION
from . import settings_service as S
from . import shopcfg as C

log = logging.getLogger("cheapedeal.launch")
SLEEP = time.sleep  # tests replace this
DEFAULT_RENDER = "cheaperdeals-api"
DEFAULT_VERCEL = "cheaperdeals-shop"
DEFAULT_DB = "cheaperdeals"
_LOCK = threading.Lock()
SECRET_KEYS = ["host_env_cj_enc", "host_env_db_enc", "host_env_admin_enc", "host_internal_key_enc"]


class Step(Exception):
    """A launch step failed; the message is for the owner."""


# ---- state / log -------------------------------------------------------------------------------------
def log_of(db) -> list[list[str]]:
    try:
        return json.loads(S.get(db, "launch_log", "") or "[]")
    except ValueError:
        return []


def state(db) -> str:
    st = S.get(db, "launch_state", "") or "idle"
    if st == "running":
        try:
            if time.time() - float(S.get(db, "launch_ts", "0") or 0) > 20 * 60:  # a restart killed the thread: do not stay "running" forever
                return "error"
        except ValueError:
            return "error"
    return st


def _log(db, msg: str, level: str = "info") -> None:
    (log.error if level == "error" else log.info)("%s", _scrub(db, msg), extra={"cat": "launch"})
    rows = log_of(db)
    rows.append([level, msg[:300]])
    S.set_value(db, "launch_log", json.dumps(rows[-60:]))
    db.commit()


def _scrub(db, text: str) -> str:
    """Hide every secret before it reaches the page (same rules as the log files)."""
    diag.refresh_secrets(db)
    return diag.redact(text)


def wanted(db) -> bool:
    """Use the launcher once a provider login was started/confirmed, and when this copy is not itself the Render server."""
    return not os.getenv("RENDER") and S.get_bool(db, "host_use_cli")


LOGIN_SLEEP = time.sleep  # tests replace this
LOGIN_POLL = 3.0


def _need_login(db, which: str, sf=None) -> dict:
    """Make sure `which` CLI is installed and logged in. Not logged in: start its browser login ourselves and wait for the person to
    allow it (so Start needs no separate login step). A CLI that is not installed cannot be fixed from here: say how to install it."""
    st = hostcli.status(which)
    if not st["installed"]:
        raise Step(f"{hostcli.LABEL[which]} CLI is computer par nahi mili. " + st["hint"])
    if st["logged_in"]:
        return st
    if sf is None:
        raise Step(f"{hostcli.LABEL[which]} mein login nahi hai. API Keys page par '{hostcli.SHORT[which]} login (CLI)' dabao, browser mein allow karo, phir Start dobara dabao.")
    _log(db, f"{hostcli.LABEL[which]} mein login nahi hai. Browser khol raha hun: wahan allow karo, main yahin intezaar kar raha hun (10 minute tak).", "warn")
    hostcli.start_login(sf, which, background=True)
    shown = False
    deadline = time.monotonic() + hostcli.LOGIN_MAX + 60
    info: dict = {}
    while time.monotonic() < deadline:
        with sf() as s2:
            info = hostcli.login_info(s2, which)
        if not shown and (info.get("url") or info.get("lines")):
            extra = " | ".join(l for l in (info.get("lines") or []) if "code" in l.lower())[:200]
            _log(db, "Browser na khule to ye link kholo: " + (info.get("url") or "(link abhi aaya nahi)") + (f"  [{extra}]" if extra else ""))
            shown = True
        if info.get("state") in ("done", "failed"):
            break
        LOGIN_SLEEP(LOGIN_POLL)
    st = hostcli.status(which)
    if st["logged_in"]:
        _log(db, f"{hostcli.LABEL[which]} login ho gaya ({st['who']}).", "ok")
        return st
    tail = " / ".join((info.get("lines") or [])[-2:])
    raise Step(f"{hostcli.LABEL[which]} login poora nahi hua ({tail or 'browser mein allow nahi kiya'}). API Keys page se dobara try karo ya terminal mein khud login karo.")


def _warn_skipped(db) -> None:
    if gitpush.SKIPPED:
        _log(db, "GitHub token ko 'workflow' permission nahi, isliye keep-alive file (" + ", ".join(gitpush.SKIPPED) + ") upload nahi hui. Zaruri nahi: server khud ko ping karta hai. Chahiye to terminal mein `gh auth refresh -h github.com -s workflow` chalao aur Start dobara dabao.", "warn")


def _wait(cond, tries: int, every: int) -> bool:
    for i in range(tries):
        if cond():
            return True
        if i < tries - 1:
            SLEEP(every)
    return False


# ---- steps -----------------------------------------------------------------------------------------------
def _match_service(db, svcs: list[dict]) -> dict | None:
    """The Render service that is ours: saved id, else the usual name, else one built from our GitHub repo."""
    sid, name = S.get(db, "host_render_service", ""), (S.get(db, "host_render_name", "") or DEFAULT_RENDER)
    repo = S.get(db, "host_repo", "").rstrip("/").lower().removesuffix(".git")
    return (next((x for x in svcs if sid and x["id"] == sid), None) or next((x for x in svcs if x["name"] == name), None)
            or next((x for x in svcs if repo and x.get("repo", "").rstrip("/").lower().removesuffix(".git").endswith(repo.split("github.com/")[-1])), None))


def detect(db, svcs: list[dict] | None = None) -> list[dict]:
    """Look at what already exists, change nothing. One entry per piece: state found / missing / unknown (+ a sentence).
    Start uses it to decide what to build; the Website jodo page shows it."""
    out: list[dict] = []

    def add(key, label, state, detail):
        out.append({"key": key, "label": label, "state": state, "detail": detail})
    if svcs is None:
        try:
            svcs = hostcli.render_services()
        except hostcli.CliError as e:
            add("render", "Render (backend)", "unknown", _scrub(db, str(e))[:200])
    if svcs is not None:
        m = _match_service(db, svcs)
        if m:
            add("render", "Render (backend)", "found", f"{m['name']} ({m['url'] or 'address nahi'})")
        else:
            add("render", "Render (backend)", "missing", f"'{S.get(db, 'host_render_name', '') or DEFAULT_RENDER}' nahi mili ({len(svcs)} services dekhi): nayi banegi")
    repo = S.get(db, "host_repo", "")
    if repo:
        add("github", "GitHub (code)", "found", f"{repo} (save hai)")
    else:
        try:
            st = hostcli.status("github")
            if st["logged_in"]:
                r = gitpush.repo_exists(hostcli.github_token(), (S.get(db, "host_render_name", "") or DEFAULT_RENDER))
                add("github", "GitHub (code)", "found" if r["exists"] else "missing", r["url"] + (" pehle se hai" if r["exists"] else " nahi hai: code upload hoga"))
            else:
                add("github", "GitHub (code)", "unknown", "login nahi / CLI nahi: zarurat padi to Start khud login karwayega")
        except (hostcli.CliError, gitpush.GitError) as e:
            add("github", "GitHub (code)", "unknown", _scrub(db, str(e))[:200])
    if C.read_secret(db, "host_env_db_enc"):
        add("neon", "Neon (database)", "found", "address save hai")
    else:
        try:
            st = hostcli.status("neon")
            if st["logged_in"]:
                p = hostcli.neon_find(DEFAULT_DB)
                add("neon", "Neon (database)", "found" if p else "missing", f"'{DEFAULT_DB}' " + ("project pehle se hai" if p else "nahi hai: free database banega"))
            else:
                add("neon", "Neon (database)", "unknown", "login nahi / CLI nahi: zarurat padi to Start khud login karwayega")
        except hostcli.CliError as e:
            add("neon", "Neon (database)", "unknown", _scrub(db, str(e))[:200])
    site = S.get(db, "host_vercel_url", "") or f"https://{S.get(db, 'host_vercel_project', '') or DEFAULT_VERCEL}.vercel.app"
    if hosting.alive(site, "/api/ping"):
        add("vercel", "Vercel (dukaan)", "found", f"{site} khuli hai aur /api se backend tak pahunchti hai")
    else:
        add("vercel", "Vercel (dukaan)", "missing", f"{site} par dukaan nahi mili: deploy hogi")
    return out


def _store_detect(db, items: list[dict]) -> None:
    S.set_value(db, "host_detect", json.dumps({"ts": time.time(), "items": items}))
    db.commit()


def _ensure_backend(db, sf=None, svcs: list[dict] | None = None) -> str:
    name = S.get(db, "host_render_name", "") or DEFAULT_RENDER
    if svcs is None:
        _log(db, "Render check: tumhari services dekh raha hun...")
        svcs = hostcli.render_services()
    svc = _match_service(db, svcs)
    if svc:
        if not svc["url"]:
            raise Step(f"Render service '{svc['name']}' ka address nahi mila.")
        S.set_value(db, "host_render_service", svc["id"])
        S.set_value(db, "host_render_url", svc["url"])
        S.set_value(db, "host_backend_url", svc["url"])
        db.commit()
        _log(db, f"Backend pehle se maujood hai: {svc['name']} ({svc['url']}). Wahi use kar raha hun.", "ok")
        url = svc["url"]
    else:
        url = _create_backend(db, name, sf)
    _log(db, "Backend jaga raha hun (free Render ko uthne mein ek minute lag sakta hai)...")
    if not _wait(lambda: hosting.alive(url), 60, 10):
        raise Step(f"Backend ({url}) /ping ka jawab nahi de raha. Render dashboard > us service > Logs dekho (build fail ya database address galat ho sakta hai), phir Start dobara dabao.")
    _log(db, "Backend jawab de raha hai.", "ok")
    _check_version(db, url)
    return url


def remote_version(url: str) -> str:
    try:
        r = C.client().get(url.rstrip("/") + "/health", timeout=15)
        return str(r.json().get("version", "")) if r.status_code == 200 else ""
    except (httpx.HTTPError, ValueError, AttributeError):
        return ""


def _check_version(db, url: str) -> None:
    rv = remote_version(url)
    S.set_value(db, "host_remote_version", rv)
    if rv == VERSION:
        return
    _log(db, f"Render par code v{rv or 'purana (version nahi batata)'} chal raha hai, is computer par v{VERSION} hai. "
             "Naya code bhejne ke liye 'Render par naya code bhejo' dabao (Website jodo page).", "warn")


def _create_backend(db, name: str, sf=None) -> str:
    _log(db, f"Render par '{name}' nahi mili. Nayi bana raha hun.", "warn")
    cj = C.read_secret(db, "host_env_cj_enc")
    if not cj:
        raise Step("CJ API key pehle API Keys page par daalo (Render par bane server ko wahi chahiye).")
    # database: Neon (free), created through its CLI; the address is saved sealed so this happens once
    if not C.read_secret(db, "host_env_db_enc"):
        _need_login(db, "neon", sf)
        _log(db, "Neon par free database bana raha hun...")
    dbu = hostcli.neon_database(db, DEFAULT_DB)
    db.commit()
    adm = C.read_secret(db, "host_env_admin_enc")
    if not adm:
        adm = secrets.token_urlsafe(14)  # Render server's admin login; shown on the Website jodo page ("Password dikhao")
        C.save_secret(db, "host_env_admin_enc", adm)
        db.commit()
    repo = S.get(db, "host_repo", "")
    if not repo:
        _need_login(db, "github", sf)
        _log(db, "Code GitHub par daal raha hun (local.env jaisi secret files nahi jaati)...")
        try:
            repo = gitpush.publish(hostcli.github_token(), name)
            _warn_skipped(db)
        except gitpush.GitError as e:
            raise Step(str(e))
        S.set_value(db, "host_repo", repo)
        db.commit()
        _log(db, f"Code yahan hai: {repo}", "ok")
    predicted = f"https://{name}.onrender.com"
    env = {"CJ_API_KEY": cj, "DATABASE_URL": dbu, "ADMIN_PASSWORD": adm, "PUBLIC_URL": predicted, "KEEPALIVE_URL": predicted,
           "DRY_RUN": "false" if S.get_bool(db, "host_live") else "true"}
    r = hostcli.render_create(db, name, repo, "main", env)
    db.commit()
    _log(db, f"Render service ban gayi: {r['url']}. Pehli build 3-5 minute leti hai.", "ok")
    if r["url"].rstrip("/") != predicted:
        _log(db, "Render ne alag address diya; keep-alive Render ke apne address se chalega (RENDER_EXTERNAL_URL).", "warn")
    return r["url"]


def _ensure_site(db, backend: str) -> str:
    proj = S.get(db, "host_vercel_project", "") or DEFAULT_VERCEL
    site = S.get(db, "host_vercel_url", "")
    _log(db, "Vercel check: tumhara project dekh raha hun...")
    if site and S.get(db, "host_vercel_backend", "") == backend and hosting.alive(site, "/api/ping"):
        S.set_value(db, "shop_site_url", site)
        db.commit()
        _log(db, f"Website pehle se maujood hai aur backend se jui hai: {site}. Wahi use kar raha hun.", "ok")
        return site
    _log(db, f"Vercel par '{proj}' deploy kar raha hun (1-3 minute)...", "warn")
    site = hostcli.vercel_deploy(db, proj, backend)["site"]  # returns only after the deployment finished
    db.commit()
    if not _wait(lambda: hosting.alive(site, "/api/ping"), 12, 5):
        raise Step(f"Dukaan {site} khuli par /api se backend tak nahi pahunch rahi. Vercel project mein 'Deployment Protection' band karo aur dobara Start dabao.")
    _log(db, f"Website live hai: {site}", "ok")
    return site


def _wake_remote(db, backend: str, site: str) -> None:
    key = C.read_secret(db, "host_internal_key_enc")
    if not key:
        _log(db, "Ye Render server maine nahi banaya, isliye usse Start nahi bhej sakta. Uske admin login mein jaakar wahan Start dabao.", "warn")
        return
    live = S.get_bool(db, "host_live")
    for _ in range(4):
        try:
            r = C.client().post(backend.rstrip("/") + "/internal/engine/start", headers={"X-Internal-Key": key}, json={"live": live, "site_url": site}, timeout=30)
            if r.status_code == 200:
                _log(db, "Render wale server ka engine Start ho gaya" + (" (LIVE: asli orders CJ ko jayenge)." if live else " (Rehearsal mode: asli orders abhi CJ ko nahi jayenge)."), "ok")
                return
            if r.status_code == 401:
                _log(db, "Render server ki internal key alag hai (shayad kisi aur ne banaya). Uske admin login se Start dabao.", "warn")
                return
        except httpx.HTTPError:
            pass
        SLEEP(5)
    raise Step("Render server ko Start ka signal nahi pahunch paya. Thodi der baad Start dobara dabao.")


def update_remote(session_factory, actor: str = "admin") -> bool:
    """Send this computer's code to GitHub; Render builds it (auto-deploy, or we ask Render to build); wait until Render reports this version."""
    if not _LOCK.acquire(blocking=False):
        return False
    db = session_factory()
    try:
        S.set_value(db, "launch_state", "running")
        S.set_value(db, "launch_ts", str(time.time()))
        S.set_value(db, "launch_log", "[]")
        db.commit()
        repo, backend = S.get(db, "host_repo", ""), S.get(db, "host_render_url", "")
        if not repo or not backend:
            raise Step("Pehle Start dabao: Render server aur GitHub repo abhi bane hi nahi hain.")
        _need_login(db, "github", session_factory)
        _log(db, f"Naya code (v{VERSION}) GitHub par bhej raha hun...")
        gitpush.publish(hostcli.github_token(), repo.rstrip("/").split("/")[-1])
        _warn_skipped(db)
        _log(db, "Code GitHub par pahunch gaya.", "ok")
        _log(db, "Render ke auto-deploy ka intezaar (3 minute tak)...")
        if not _wait(lambda: remote_version(backend) == VERSION, 18, 10):
            _log(db, "Auto-deploy nahi dikha. Render ko seedha build karne bol raha hun...", "warn")
            _need_login(db, "render", session_factory)
            sid = S.get(db, "host_render_service", "")
            if not sid:
                raise Step("Render service ka ID save nahi hai. Render dashboard mein 'Manual Deploy > Deploy latest commit' dabao.")
            hostcli.render_deploy(sid)
            if not _wait(lambda: remote_version(backend) == VERSION, 60, 10):
                raise Step("Render par naya code abhi tak nahi chala. Render dashboard > service > Logs mein build ki galti dekho.")
        S.set_value(db, "host_remote_version", VERSION)
        S.set_value(db, "launch_state", "ok")
        _log(db, f"Render par ab v{VERSION} chal raha hai.", "ok")
        return True
    except (Step, hostcli.CliError, gitpush.GitError) as e:
        db.rollback()
        S.set_value(db, "launch_state", "error")
        _log(db, _scrub(db, str(e)), "error")
    except Exception as e:
        db.rollback()
        S.set_value(db, "launch_state", "error")
        log.exception("update crashed", extra={"cat": "launch"})
        _log(db, _scrub(db, f"Andar ki galti: {type(e).__name__}: {e}")[:250] + " (Diagnostics page par poora trace hai)", "error")
    finally:
        db.close()
        _LOCK.release()
    return False


def preflight(db) -> list[dict]:
    """A check of everything Start needs, WITHOUT changing anything: what is ready, what is missing, what to do about it."""
    items: list[dict] = []

    def add(name, ok, detail, fix=""):
        items.append({"name": name, "ok": ok, "detail": detail, "fix": fix})
    cj = bool(C.read_secret(db, "host_env_cj_enc"))
    add("CJ API key", cj, "save hai" if cj else "daali nahi gayi", "" if cj else "API Keys page par CJ key daalo.")
    sts = {}
    for w in hostcli.PROVIDERS:
        st = hostcli.status(w)
        sts[w] = st
        S.set_value(db, f"cli_status_{w}", json.dumps(st))
    have_backend = bool(S.get(db, "host_render_service", ""))
    needs = {"vercel": "dukaan deploy karne ke liye", "render": "backend dhundne/banane ke liye",
             "github": "" if S.get(db, "host_repo", "") else "code upload ke liye (pehli baar)",
             "neon": "" if (C.read_secret(db, "host_env_db_enc") or have_backend) else "free database ke liye (pehli baar)"}
    for w in hostcli.PROVIDERS:
        st = sts[w]
        why = needs[w]
        if st["logged_in"]:
            add(hostcli.LABEL[w], True, f"login hai ({st['who']})")
        elif not why:
            add(hostcli.LABEL[w], None, "abhi zarurat nahi (pehle se bana hua hai)")
        elif not st["installed"]:
            add(hostcli.LABEL[w], False, f"CLI nahi mili, {why} chahiye", st["hint"] or hostcli.INSTALL[w])
        else:
            add(hostcli.LABEL[w], False, f"login nahi hai, {why} chahiye", f"API Keys page par '{hostcli.SHORT[w]} login (CLI)' dabao.")
    svcs = None
    if sts["render"]["logged_in"]:
        try:
            svcs = hostcli.render_services()
        except hostcli.CliError as e:
            add("Render access", False, _scrub(db, str(e))[:250], "Upar ka error padho; workspace ka masla ho to terminal mein `render workspace set`.")
    if svcs is not None or not sts["render"]["logged_in"]:
        found = detect(db, svcs) if svcs is not None else [i for i in detect(db, []) if i["key"] != "render"]
        _store_detect(db, found)
        for i in found:
            add("Detection: " + i["label"], True if i["state"] == "found" else None, ("mila: " if i["state"] == "found" else ("nahi mila: " if i["state"] == "missing" else "pata nahi: ")) + i["detail"])
        m = _match_service(db, svcs) if svcs else None
        if m and m["url"] and hosting.alive(m["url"]):
            rv = remote_version(m["url"])
            add("Render ka code version", rv == VERSION, f"Render par v{rv or 'purana'}, yahan v{VERSION}", "" if rv == VERSION else "Website jodo page par 'Render par naya code bhejo' dabao.")
    S.set_value(db, "host_preflight", json.dumps({"ts": time.time(), "checks": items}))
    db.commit()
    return items


# ---- entry points ----------------------------------------------------------------------------------------------
def run(session_factory, actor: str = "admin") -> bool:
    """Whole launch. Safe to call from a thread. Returns True when everything is up and the local engine started."""
    if not _LOCK.acquire(blocking=False):
        return False
    db = session_factory()
    try:
        S.set_value(db, "launch_state", "running")
        S.set_value(db, "launch_ts", str(time.time()))
        S.set_value(db, "launch_log", "[]")
        db.commit()
        _need_login(db, "vercel", session_factory)
        _need_login(db, "render", session_factory)
        _log(db, "Vercel aur Render CLI login theek hai.")
        _log(db, "Detection: dekh raha hun kya-kya pehle se bana hai...")
        svcs = hostcli.render_services()
        found = detect(db, svcs)
        _store_detect(db, found)
        word = {"found": "mila", "missing": "nahi mila", "unknown": "pata nahi"}
        _log(db, "Detection: " + " | ".join(f"{i['label']}: {word[i['state']]}" for i in found))
        todo = [i["label"] for i in found if i["state"] == "missing"]
        _log(db, ("Jo nahi mila wo ab khud banaunga: " + ", ".join(todo) + ".") if todo else "Sab kuch pehle se hai, bas jod kar Start kar raha hun.", "ok" if not todo else "info")
        backend = _ensure_backend(db, session_factory, svcs)
        site = _ensure_site(db, backend)
        _wake_remote(db, backend, site)
        engine.start(db, actor)
        S.set_value(db, "launch_state", "ok")
        _log(db, f"Sab ready. Dukaan: {site} | Backend/admin: {backend}", "ok")
        return True
    except (Step, hostcli.CliError, gitpush.GitError) as e:
        db.rollback()
        S.set_value(db, "launch_state", "error")
        _log(db, _scrub(db, str(e)), "error")
    except Exception as e:  # never leave the page stuck on "running"
        db.rollback()
        S.set_value(db, "launch_state", "error")
        log.exception("launch crashed", extra={"cat": "launch"})
        _log(db, _scrub(db, f"Andar ki galti: {type(e).__name__}: {e}")[:250] + " (Diagnostics page par poora trace hai)", "error")
    finally:
        db.close()
        _LOCK.release()
    return False


def begin(session_factory, actor: str, background: bool) -> None:
    if background:
        threading.Thread(target=run, args=(session_factory, actor), daemon=True, name="launch").start()
    else:
        run(session_factory, actor)
