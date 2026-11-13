"""Connect Vercel and Render through their own command-line tools (no API key / token needed).

You log in once in the browser (the CLI stores that login on this computer, not in this app). After that the app runs
`vercel link/deploy` and `render services/deploys` for you. Requires the CLIs on this computer:
  Vercel: Node.js (the app uses `vercel`, or `npx vercel`)        Render: the Render CLI (render.com/docs/cli)
The commands/flags follow the official docs; they were NOT run live from the build sandbox (tests use a fake runner)."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from . import diag, hosting
from . import settings_service as S

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
URL = re.compile(r"https?://[^\s\"'<>)\]]+")


log = logging.getLogger("cheapedeal.cli")


class CliError(Exception):
    """A CLI step failed; message is for the owner."""


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 300) -> tuple[int, str, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


RUN = _run  # tests replace this
POPEN = subprocess.Popen  # tests replace this


def clean(text: str) -> str:
    return ANSI.sub("", text or "").replace("\r", "\n")


PROVIDERS = ("vercel", "render", "github", "neon")
LABEL = {"vercel": "Vercel (frontend)", "render": "Render (backend)", "github": "GitHub (code)", "neon": "Neon (database)"}
SHORT = {w: LABEL[w].split(" ")[0] for w in LABEL}
STATUS_ARGS = {"vercel": ["whoami"], "render": ["whoami", "-o", "text"], "github": ["auth", "status"], "neon": ["me", "--output", "json"]}
LOGIN_ARGS = {"vercel": ["login"], "render": ["login"], "github": ["auth", "login", "--web", "-h", "github.com", "-p", "https", "-s", "repo,workflow"], "neon": ["login"]}


# `neonctl@latest` (v9.x) is a thin wrapper whose dependency `neon` -> `@neon/config@1.14.0` was not installable on the owner's
# machine (npm ETARGET). So `npx` uses an older stand-alone release first; the first one that starts is remembered.
NEON_VERSIONS = ("7.0.8", "2.29.3", "latest")
_NEON_PICK: list[str] = []


def _neon_npx(npx: str) -> list[str]:
    if _NEON_PICK:
        return [npx, "--yes", f"neonctl@{_NEON_PICK[0]}"]
    for v in NEON_VERSIONS:
        try:
            code, _o, _e = RUN([npx, "--yes", f"neonctl@{v}", "--version"], timeout=240)
        except (OSError, subprocess.TimeoutExpired):
            break  # cannot even run npx here: use the first pin, the real error shows up when the command runs
        if code == 0:
            _NEON_PICK.append(v)
            return [npx, "--yes", f"neonctl@{v}"]
    _NEON_PICK.append(NEON_VERSIONS[0])  # none started: stop re-probing (each probe can take minutes); the real command shows the real error
    return [npx, "--yes", f"neonctl@{NEON_VERSIONS[0]}"]


def cmd_for(which: str) -> list[str] | None:
    # Always the FULL path from shutil.which: on Windows "npx"/"vercel" are .cmd files and a bare name gives WinError 2.
    npx = shutil.which("npx")
    if which == "vercel":
        v = shutil.which("vercel")
        return [v] if v else ([npx, "--yes", "vercel@latest"] if npx else None)
    if which == "neon":
        n = shutil.which("neon") or shutil.which("neonctl")
        return [n] if n else (_neon_npx(npx) if npx else None)
    exe = shutil.which({"render": "render", "github": "gh"}[which])
    return [exe] if exe else None


INSTALL = {"vercel": "Node.js install karo (nodejs.org), phir terminal mein:  npm i -g vercel",
           "render": "Render CLI install karo (render.com/docs/cli: Windows ke liye GitHub releases se render.exe, PATH mein rakho)",
           "github": "GitHub CLI install karo (cli.github.com, ya terminal mein: winget install GitHub.cli)",
           "neon": "Node.js install karo, phir terminal mein:  npm i -g neonctl@7.0.8   (docs: neon.com/docs/reference/neon-cli)"}


def _need(which: str) -> list[str]:
    c = cmd_for(which)
    if c is None:
        raise CliError(f"{which.capitalize()} CLI is computer par nahi mili. {INSTALL[which]}")
    return c


def _exec(cmd: list[str], cwd: str | None = None, timeout: int = 300, both: bool = False) -> str:
    t0 = time.time()
    try:
        code, out, err = RUN(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("cli timeout after %ss: %s", timeout, " ".join(cmd[:4]), extra={"cat": "cli"})
        raise CliError(f"'{' '.join(cmd[:3])}' bahut der laga raha hai (timeout).")
    except OSError as e:
        log.warning("cli could not start: %s | %s", " ".join(cmd[:4]), e, extra={"cat": "cli"})
        raise CliError(f"CLI chal nahi payi: {e}")
    diag.log_cli(cmd, code, time.time() - t0, out, err)
    if code != 0:
        raise CliError(f"'{' '.join(cmd[:4])}' fail hua: " + (clean(err) or clean(out)).strip()[-300:])
    return clean(out + "\n" + err) if both else clean(out)


# ---- status ---------------------------------------------------------------------------------------------
def _who(which: str, text: str) -> str:
    text = clean(text)
    if which == "github":
        m = re.search(r"account\s+(\S+)", text)
        return m.group(1) if m else "github.com"
    if which == "neon":
        try:
            j = json.loads(text)
            return str(j.get("email") or j.get("login") or j.get("name") or "ok")[:80]
        except ValueError:
            pass
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1][:80] if lines else ""


def status(which: str) -> dict:
    """{installed, logged_in, who}: asks the CLI itself (vercel whoami / render whoami / gh auth status / neon me)."""
    if which == "render":
        repair_render_cfg()
    c = cmd_for(which)
    if c is None:
        return {"installed": False, "logged_in": False, "who": "", "hint": INSTALL[which]}
    t0 = time.time()
    try:
        code, out, err = RUN(c + STATUS_ARGS[which], timeout=240)  # first `npx` run downloads the CLI
        diag.log_cli(c + STATUS_ARGS[which], code, time.time() - t0, out, err)
    except subprocess.TimeoutExpired:
        log.warning("cli status timeout: %s", which, extra={"cat": "cli"})
        return {"installed": True, "logged_in": False, "who": "", "hint": "CLI ne jawab nahi diya (pehli baar download mein der lagti hai). Thodi der baad dobara check karo."}
    except OSError as e:
        log.warning("cli status could not start %s: %s", which, e, extra={"cat": "cli"})
        return {"installed": False, "logged_in": False, "who": "", "hint": f"CLI chal nahi payi ({e}). " + INSTALL[which]}
    text = (out or "") + "\n" + (err or "") if which == "github" else out  # gh prints its status on stderr
    ok = code == 0 and bool(clean(text).strip())
    return {"installed": True, "logged_in": ok, "who": _who(which, text) if ok else "", "hint": ""}


# ---- login (browser) ------------------------------------------------------------------------------------
LOGIN_MAX = 600  # seconds a browser login may take before we stop waiting for it
_LOGGING_IN: set[str] = set()
_LOGIN_LOCK = threading.Lock()


def _store_login(db_factory, which: str, lines: list[str], state: str) -> None:
    db = db_factory()
    try:
        urls = [u for l in lines for u in URL.findall(l)]
        S.set_value(db, f"cli_login_{which}", json.dumps({"state": state, "lines": lines[-12:], "url": urls[0] if urls else "", "ts": time.time()}))
        db.commit()
    finally:
        db.close()


def start_login(db_factory, which: str, background: bool = True) -> None:
    """Runs `vercel login` / `render login`, which opens the browser; we capture its output so the page can show the link/code."""
    if which == "render":
        repair_render_cfg()
    c = _need(which)
    cmd = c + LOGIN_ARGS[which]
    with _LOGIN_LOCK:
        if which in _LOGGING_IN:  # double click: the first login is still waiting for the browser
            return
        _LOGGING_IN.add(which)

    def work():
        lines: list[str] = []
        timer = None
        try:
            p = POPEN(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE if which == "github" else subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
            if which == "github" and getattr(p, "stdin", None):  # gh waits for "Press Enter to open github.com in your browser"
                try:
                    p.stdin.write("\n\n")
                    p.stdin.flush()
                    p.stdin.close()
                except OSError:
                    pass
            timer = threading.Timer(LOGIN_MAX, lambda: _kill(p))  # a login nobody finishes must not hang this thread for ever
            timer.daemon = True
            timer.start()
            for raw in p.stdout:
                for l in clean(raw).splitlines():
                    if l.strip():
                        lines.append(l.strip()[:200])
                _store_login(db_factory, which, lines, "running")
            code = p.wait(timeout=30)
            if code != 0:
                lines.append("Login fail hua. CLI purani ho to update karo, ya terminal mein khud chalao: " + " ".join(["gh auth login" if which == "github" else ("neon login" if which == "neon" else which + " login")]) + "  phir yahan Status check dabao.")
            _store_login(db_factory, which, lines, "done" if code == 0 else "failed")
            (log.info if code == 0 else log.warning)("cli login %s exit %s | %s", which, code, " / ".join(lines[-4:]), extra={"cat": "cli"})
        except Exception as e:
            log.warning("cli login %s crashed: %s: %s", which, type(e).__name__, e, extra={"cat": "cli"})
            lines.append(f"{type(e).__name__}: {e}"[:200])
            if isinstance(e, OSError):
                lines.append(INSTALL[which])
            _store_login(db_factory, which, lines, "failed")
        finally:
            if timer:
                timer.cancel()
            with _LOGIN_LOCK:
                _LOGGING_IN.discard(which)

    _store_login(db_factory, which, [], "running")
    if background:
        threading.Thread(target=work, daemon=True, name=f"login-{which}").start()
    else:
        work()


def _kill(p) -> None:
    try:
        p.kill()
    except Exception:
        pass


def login_info(db, which: str) -> dict:
    try:
        d = json.loads(S.get(db, f"cli_login_{which}", "") or "{}")
    except ValueError:
        return {}
    if d.get("state") == "running" and which not in _LOGGING_IN:  # the app restarted (or the thread died) mid-login: do not show "running" for ever
        d["state"] = "failed"
        d["lines"] = (d.get("lines") or []) + ["Login beech mein ruk gaya (app band/restart hui ya time khatam). Login button dobara dabao."]
    return d


# ---- Vercel ---------------------------------------------------------------------------------------------
def vercel_site(output: str, project: str) -> str:
    """The public address of the deployed shop. Normally https://<project>.vercel.app, but when that name belongs to someone else Vercel
    gives another alias; the CLI prints it on an 'Aliased:' line, so prefer that, then the plain project address."""
    m = re.search(r"Aliased:\s*(https://[^\s\[]+)", output or "")
    if m:
        return m.group(1).rstrip(".,/")
    plain = f"https://{project}.vercel.app"
    return plain  # "Production:" is the one-off deployment address (changes every deploy), not the stable shop address


def vercel_deploy(db, project: str, backend_url: str) -> dict:
    """Write the shop into a temp folder, link/create the Vercel project by name, deploy to production. Returns {site, url}."""
    if not hosting.valid_project_name(project):
        raise CliError("Project ka naam chhote akshar, number aur '-' mein hona chahiye (jaise my-shop).")
    if not backend_url.startswith("https://"):
        raise CliError("Backend ka https:// address chahiye (Render ka URL). Pehle backend jodo.")
    c = _need("vercel")
    scope = (S.get(db, "vercel_team", "") or "").strip()
    sc = ["--scope", scope] if scope else []
    tmp = Path(tempfile.mkdtemp(prefix="cheaperdeals_site_")) / project
    try:
        tmp.mkdir(parents=True)
        for f in hosting.site_files(backend_url):
            (tmp / f["file"]).write_text(f["data"], encoding="utf-8")
        _exec(c + ["link", "--yes", "--project", project] + sc, cwd=str(tmp), timeout=180)
        out = _exec(c + ["deploy", "--prod", "--yes"] + sc, cwd=str(tmp), timeout=900, both=True)  # the "Aliased:" line is on stderr
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    urls = [u.rstrip(".,") for u in URL.findall(out) if "vercel.app" in u]
    site = vercel_site(out, project)
    S.set_value(db, "host_vercel_project", project)
    S.set_value(db, "host_vercel_url", site)
    S.set_value(db, "host_vercel_backend", backend_url.rstrip("/"))
    S.set_value(db, "host_backend_url", backend_url.rstrip("/"))
    S.set_value(db, "shop_site_url", site)
    return {"site": site, "url": urls[-1] if urls else site}


# ---- Render ---------------------------------------------------------------------------------------------
def _json_in(text: str):
    """First JSON list/object inside `text`, even when the CLI prints a banner, warning or update notice before/after it."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    dec = json.JSONDecoder()
    empty = None
    for m in re.finditer(r"[\[{]", text):
        try:
            v, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(v, (list, dict)):
            if v:
                return v
            empty = v if empty is None else empty
    if empty is not None:
        return empty
    raise ValueError("no json")


def _json_safe(text: str):
    try:
        return _json_in(clean(text).strip())
    except ValueError:
        return None


def _table_rows(text: str) -> list[dict]:
    """Rows of a text table (box drawing | or spaces) as dicts with lower-case header names. Some CLI versions ignore --output json."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip() or re.fullmatch(r"[\s\-+=─━┌┐└┘├┤┬┴┼╭╮╰╯]+", line):
            continue
        if re.search(r"[│|┃║]", line):
            cells = [c.strip() for c in re.split(r"[│|┃║]", line)]
        else:
            cells = re.split(r"\s{2,}", line.strip())
        cells = [c for c in cells if c != ""]
        if cells:
            rows.append(cells)
    for i, r in enumerate(rows):
        low = [c.lower() for c in r]
        if "id" in low and "name" in low:
            return [dict(zip(low, c)) for c in rows[i + 1:] if len(c) == len(low)]
    return []


def json_items(out: str, keys: tuple[str, ...], what: str, cmd: str) -> list:
    """List out of a CLI's answer. An account with nothing yet prints `null`, `[]`, `{}` or even nothing: all of those mean 'empty list',
    not an error (a `null` here used to crash the whole Start with "'NoneType' object has no attribute 'get'").
    Also tolerated: banner/warning lines around the JSON, and a plain text table instead of JSON."""
    text = clean(out).strip()
    if not text:
        return []
    try:
        j = _json_in(text)
    except ValueError:
        if text.lower() in ("null", "[]", "{}") or re.search(r"\bno (services|projects|workspaces|results)\b|nothing found|not found|0 projects", text, re.I):
            return []
        rows = _table_rows(text)
        if rows:
            return rows
        snippet = re.sub(r"\s+", " ", text)[:160]
        raise CliError(f"{what} ka jawab padh nahi paya. Terminal mein `{cmd}` chala kar dekho. CLI ne ye likha: {snippet}")
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        for k in keys:
            v = j.get(k)
            if isinstance(v, list):
                return v
        return []
    return []  # null / number / string


def _svc(d: dict) -> dict | None:
    d = d.get("service", d) if isinstance(d, dict) else {}
    if not d.get("id"):
        return None
    det = d.get("serviceDetails") or {}
    url = det.get("url") or d.get("url") or ""
    if url and not url.startswith("http"):
        url = "https://" + url
    r = d.get("repo") or d.get("repoUrl") or det.get("repo") or ""
    repo = str(r.get("url", "") if isinstance(r, dict) else r)
    return {"id": d["id"], "name": d.get("name", ""), "type": d.get("type", ""), "url": url, "repo": repo}


# ---- Render workspace: the CLI refuses every command ("no workspace set") until a workspace was picked once. ----------------
# Verified from the CLI's source (pkg/config/config.go): ~/.render/cli.yaml keeps it FLAT as `workspace: <id>` + `workspace_name: <name>`
# (override of the file path: RENDER_CLI_CONFIG_PATH), and the env var RENDER_WORKSPACE=<id> overrides the file. v14.2 wrongly wrote
# `workspace:` as a nested map, which made EVERY render command (even login) fail with "yaml: unmarshal errors ... !!map into string".
# repair_render_cfg() fixes such a file; the app now uses the env var first and writes the flat format.
def render_cfg_path() -> Path:
    return Path(os.environ.get("RENDER_CLI_CONFIG_PATH") or (Path.home() / ".render" / "cli.yaml")).expanduser()


def _top_key(line: str, key: str) -> bool:
    return re.match(rf"^{key}\s*:", line) is not None


def _strip_top_keys(lines: list[str], keys: tuple[str, ...]) -> list[str]:
    """Drop top-level keys (and their indented children, if any); every other line stays untouched."""
    out: list[str] = []
    skipping = False
    for line in lines:
        if any(_top_key(line, k) for k in keys):
            skipping = True
            continue
        if skipping and (line.startswith((" ", "\t")) or not line.strip()):
            continue
        skipping = False
        out.append(line)
    return out


def repair_render_cfg() -> bool:
    """If an older version of this app wrote `workspace:` as a nested map, turn it into the flat form the Render CLI reads. True if changed."""
    path = render_cfg_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError:
        return False
    wid = wname = ""
    broken = False
    for i, line in enumerate(lines):
        if re.match(r"^workspace\s*:\s*(#.*)?$", line):
            broken = True
            for child in lines[i + 1:]:
                if not child.startswith((" ", "\t")) and child.strip():
                    break
                m = re.match(r"\s+id\s*:\s*(.+?)\s*$", child)
                if m:
                    wid = m.group(1).strip("\"'")
                m = re.match(r"\s+name\s*:\s*(.+?)\s*$", child)
                if m:
                    try:
                        wname = json.loads(m.group(1))
                    except ValueError:
                        wname = m.group(1).strip("\"'")
    if not broken:
        return False
    keep = _strip_top_keys(lines, ("workspace", "workspace_name"))
    if wid:
        keep += [f"workspace: {wid}", f"workspace_name: {json.dumps(wname or wid)}"]
    try:
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except OSError:
        return False
    log.warning("render cli.yaml repaired (nested workspace -> flat)", extra={"cat": "cli"})
    return True


def render_workspaces() -> list[dict]:
    out = _exec(_need("render") + ["workspaces", "-o", "json", "--confirm"], timeout=120)
    items = json_items(out, ("workspaces", "items"), "Render workspaces", "render workspaces -o json")
    res = []
    for w in items:
        w = w.get("workspace", w) if isinstance(w, dict) else {}
        if w.get("id"):
            res.append({"id": str(w["id"]), "name": str(w.get("name") or w["id"])})
    return res


def render_set_workspace(ws: dict) -> None:
    """Use this workspace: RENDER_WORKSPACE for every CLI we start, and (best effort) the flat keys in the CLI's own config so a terminal works too."""
    os.environ["RENDER_WORKSPACE"] = ws["id"]
    path = render_cfg_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        keep = _strip_top_keys(lines, ("workspace", "workspace_name")) + [f"workspace: {ws['id']}", f"workspace_name: {json.dumps(ws['name'])}"]  # JSON string is valid YAML
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("render config not written (%s); RENDER_WORKSPACE is used instead", e, extra={"cat": "cli"})


def _render(args: list[str], timeout: int = 120) -> str:
    """Run a render command; if the CLI says no workspace is set, pick the only one automatically and retry once."""
    repair_render_cfg()
    try:
        return _exec(_need("render") + args, timeout=timeout)
    except CliError as e:
        if "no workspace" not in str(e).lower():
            raise
        spaces = render_workspaces()
        if len(spaces) != 1:
            names = ", ".join(w["name"] for w in spaces) or "koi nahi mila"
            raise CliError(f"Render CLI mein workspace set nahi hai aur tumhare paas workspaces hain: {names}. Terminal (cmd) mein ek baar `render workspace set` chalao, apna workspace chuno, phir Start dabao.")
        render_set_workspace(spaces[0])
        try:
            return _exec(_need("render") + args, timeout=timeout)
        except CliError as e2:
            if "no workspace" in str(e2).lower():
                raise CliError(f"Workspace '{spaces[0]['name']}' set kiya par CLI ne nahi padha (config: {render_cfg_path()}). Terminal mein ek baar `render workspace set` chalao, phir Start dabao.")
            raise


def render_services() -> list[dict]:
    out = _render(["services", "-o", "json", "--confirm"], timeout=120)
    items = json_items(out, ("services", "items"), "Render CLI", "render services -o json")
    svcs = [s for s in (_svc(i) for i in items) if s]
    return [s for s in svcs if s["type"] in ("", "web_service", "web")]


def render_create(db, name: str, repo: str, branch: str, env: dict[str, str]) -> dict:
    """`render services create` with the free plan. root folder is handled by `cd api &&` because the flag for it is not in the docs I could verify."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", name or ""):
        raise CliError("Service ka naam chhote akshar/number/'-' mein likho.")
    if not repo.startswith("https://github.com/"):
        raise CliError("GitHub repo ka address chahiye (https://github.com/tum/repo).")
    import secrets as _sec
    internal = _sec.token_urlsafe(40)
    base = {"APP_ENV": "production", "PYTHON_VERSION": "3.12.7", "INTERNAL_SCHEDULER": "true", "DRY_RUN": "true", "ENABLE_DEV_TOOLS": "false", "APP_TIMEZONE": "Asia/Kolkata",
            "SECRET_KEY": _sec.token_urlsafe(40), "INTERNAL_API_KEY": internal, "ADMIN_USERNAME": "admin"}
    base.update({k: v for k, v in env.items() if v})
    cmd = ["services", "create", "--name", name, "--type", "web_service", "--runtime", "python", "--repo", repo, "--branch", branch or "main",
                             "--plan", "free", "--build-command", "cd api && pip install -r requirements.txt",
                             "--start-command", "cd api && uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1 --no-access-log --proxy-headers --forwarded-allow-ips=*"]
    for k, v in base.items():
        cmd += ["--env-var", f"{k}={v}"]
    try:
        out = _render(cmd + ["-o", "json", "--confirm"], timeout=300)
    except CliError as e:
        raise CliError(str(e) + f"  |  Seedha tareeka: dashboard.render.com/blueprint/new?repo={repo} kholo (render.yaml apne aap padh leta hai).")
    sv = None
    try:
        sv = _svc(json.loads(out))
    except ValueError:
        pass
    if not sv or not sv["url"]:
        sv = next((s for s in render_services() if s["name"] == name), None)
    if not sv:
        raise CliError("Service bani par uska address nahi mila. Render dashboard mein dekho.")
    S.set_value(db, "host_render_service", sv["id"])
    S.set_value(db, "host_render_url", sv["url"])
    S.set_value(db, "host_backend_url", sv["url"])
    from . import shopcfg as C
    C.save_secret(db, "host_internal_key_enc", internal)
    return sv


def render_deploy(service_id: str) -> None:
    """Ask Render to build the latest GitHub code now (used when auto-deploy did not pick up a push)."""
    _render(["deploys", "create", service_id, "--confirm", "-o", "text"], timeout=120)


# ---- GitHub: reuse the CLI's own login token for the REST upload (never stored by this app) -------------------------
def github_token() -> str:
    tok = _exec(_need("github") + ["auth", "token"], timeout=60).strip().splitlines()
    if not tok or not tok[-1].strip():
        raise CliError("GitHub CLI ne token nahi diya. 'GitHub login (CLI)' dabao.")
    return tok[-1].strip()


# ---- Neon: free Postgres created and read through the CLI ----------------------------------------------------------
_NEON_ORG: list[str] = []  # the organization id once we know it (neonctl asks "What organization would you like to use?" otherwise)
_ORG_PROMPT = re.compile(r"what organization|select an organization|--org-id|org_id is required|organization (?:id )?is required", re.I)


def neon_orgs(c: list[str]) -> list[dict]:
    """Organizations of the logged-in Neon account (empty when this neonctl has no such command)."""
    try:
        out = _exec(c + ["orgs", "list", "--output", "json"], timeout=120, both=True)
        return [o for o in json_items(out, ("organizations", "orgs"), "Neon CLI", "neon orgs list --output json") if isinstance(o, dict) and o.get("id")]
    except CliError:
        return []


def _neon_org(c: list[str], seen: str = "") -> str:
    ids = re.findall(r"\((org-[\w-]+)\)", seen) + re.findall(r"\b(org-[a-z0-9]+(?:-[a-z0-9]+)+)\b", seen)
    if ids:
        return ids[0]
    orgs = neon_orgs(c)
    return str(orgs[0]["id"]) if orgs else ""


def _neon_run(c: list[str], args: list[str], timeout: int = 180) -> str:
    """Run a neonctl command that may need an organization. neonctl then waits on an arrow-key menu ("What organization would you like to use?")
    that nobody can answer from here, so: use the known org id, or find it (orgs list / the prompt text itself) and run again with --org-id."""
    org = _NEON_ORG[0] if _NEON_ORG else ""
    out = _exec(c + args + (["--org-id", org] if org else []), timeout=timeout, both=True)
    if not org and _ORG_PROMPT.search(out):
        org = _neon_org(c, out)
        if not org:
            raise CliError("Neon organization poochh raha hai aur main chun nahi paya. Terminal mein `neonctl set-context --org-id <org-id>` chalao (org id: Neon dashboard > Organization settings), phir Start dobara dabao.")
        _NEON_ORG[:] = [org]
        log.info("neon organization chuni: %s", org, extra={"cat": "cli"})
        out = _exec(c + args + ["--org-id", org], timeout=timeout, both=True)
    return out


def neon_projects(c: list[str]) -> list[dict]:
    out = _neon_run(c, ["projects", "list", "--output", "json"], timeout=180)
    items = json_items(out, ("projects", "shared"), "Neon CLI", "neon projects list --output json")
    d = _json_safe(out)
    if isinstance(d, dict):  # newer neonctl: {"projects": [...], "shared": [...]}
        items = list(d.get("projects") or []) + list(d.get("shared") or [])
    return [p for p in items if isinstance(p, dict)]


def neon_find(name: str = "cheaperdeals") -> dict | None:
    """Look only (nothing is created): the Neon project called `name`, if the account has one."""
    return next((p for p in neon_projects(_need("neon")) if p.get("name") == name), None)


def neon_database(db, name: str = "cheaperdeals") -> str:
    """Return a postgresql:// URL for a Neon project called `name` (created if missing). The URL is saved sealed so later runs need no Neon login."""
    from . import shopcfg as C
    saved = C.read_secret(db, "host_env_db_enc")
    if saved:
        return saved
    c = _need("neon")
    items = neon_projects(c)
    proj = next((p for p in items if isinstance(p, dict) and p.get("name") == name), None)
    if proj:
        lines = _exec(c + ["connection-string", "--project-id", proj["id"]], timeout=120).strip().splitlines()
        url = next((l.strip() for l in lines if l.strip().startswith("postgres")), lines[-1].strip() if lines else "")
    else:
        out = _neon_run(c, ["projects", "create", "--name", name, "--output", "json"], timeout=300)
        try:
            url = _json_in(out)["connection_uris"][0]["connection_uri"]
        except (ValueError, KeyError, IndexError, TypeError):
            m = re.search(r"postgres(?:ql)?://\S+", out)
            url = m.group(0) if m else ""
        if not url:
            raise CliError("Neon project bana par connection string nahi mili. Neon dashboard se copy karke dekho.")
    if not url.startswith("postgres"):
        raise CliError("Neon se sahi database address nahi mila.")
    C.save_secret(db, "host_env_db_enc", url)
    return url
