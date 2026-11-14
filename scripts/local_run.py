r"""One-command local setup + start for Windows (also works on Linux/macOS).

    py -3 scripts\local_run.py            # first run installs everything, then starts and opens the browser
    py -3 scripts\local_run.py --setup-only
    py -3 scripts\local_run.py --reset-password

Runs on your own PC with SQLite and DRY-RUN (rehearsal) ON until you switch Live on. Nothing is simulated: without a CJ key the app says so and
holds, it never invents supplier orders or demo products. On Ctrl+C it tidies its own leftovers and says goodbye.
The virtual environment, database and secrets are kept in your home folder (C:\Users\<you>\CheapeDeal-data), NOT inside
OneDrive, so OneDrive never syncs a live database or your passwords.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import socket
import string
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = ROOT / "api"


def data_dir() -> Path:
    """Private folder for the venv, database and secrets.

    NOT %LOCALAPPDATA%: Microsoft Store Python silently redirects AppData writes into its own sandbox folder, which breaks
    virtual environments. NOT OneDrive: it would sync a live database and your password. The user's home folder is neither.
    Override with the CHEAPEDEAL_HOME environment variable.
    """
    override = os.environ.get("CHEAPEDEAL_HOME")
    d = Path(override) if override else Path.home() / "CheapeDeal-data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def say(msg: str = "") -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    say("\nERROR: " + msg)
    sys.exit(1)


def venv_python(vdir: Path) -> Path:
    return vdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def local_requirements() -> str:
    """Production pins minus what a local SQLite run does not need (Postgres driver, uvicorn extras that need a C compiler)."""
    out = []
    for line in (API / "requirements.txt").read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.lower().startswith("psycopg"):
            continue
        if s.lower().startswith("uvicorn"):
            s = "uvicorn==" + s.split("==", 1)[1]
        out.append(s)
    return "\n".join(out) + "\n"


def _venv_ok(py: Path) -> bool:
    try:
        return subprocess.run([str(py), "-c", "import sys"], capture_output=True).returncode == 0
    except OSError:
        return False


def ensure_venv(dd: Path) -> Path:
    vdir = dd / "venv"
    py = venv_python(vdir)
    if (py.exists() or py.is_symlink()) and not _venv_ok(py):
        say("Existing environment is broken; rebuilding it...")
        shutil.rmtree(vdir, ignore_errors=True)
        (dd / "installed.stamp").unlink(missing_ok=True)
    if not py.exists():
        say("[1/4] Creating virtual environment (one time)...")
        venv.EnvBuilder(with_pip=True, clear=False).create(str(vdir))
        if not _venv_ok(py):
            shutil.rmtree(vdir, ignore_errors=True)
            fail(f"The new environment cannot start ({py}).\nTry again with a normal python.org install of Python 3.12 "
                 "(tick 'Add python.exe to PATH'), or set CHEAPEDEAL_HOME to a folder like C:\\CheapeDeal-data.")
    reqs = local_requirements()
    stamp = dd / "installed.stamp"
    want = hashlib.sha256((reqs + sys.version).encode()).hexdigest()
    if stamp.exists() and stamp.read_text() == want:
        say("[2/4] Packages already installed.")
        return py
    say("[2/4] Installing packages (first run takes 1-3 minutes)...")
    req_file = dd / "requirements.local.txt"
    req_file.write_text(reqs, encoding="utf-8")
    r = subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(req_file)])
    if r.returncode != 0:
        fail("Package install failed. Check your internet connection, then run this again.\n"
             "If it keeps failing, use Python 3.11, 3.12 or 3.13 from python.org.")
    stamp.write_text(want)
    return py


def read_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def ensure_env(dd: Path, reset_password: bool) -> tuple[dict[str, str], bool]:
    path = dd / "cheapedeal.env"
    created = False
    if not path.exists() or reset_password:
        alnum = string.ascii_letters + string.digits
        old = read_env(path) if path.exists() else {}
        pw = "".join(secrets.choice(alnum) for _ in range(14))
        db = (dd / "cheapedeal.db").as_posix()
        lines = [
            "# Local settings for run_local.bat. Safe to delete: a new file is created on the next run.",
            "APP_ENV=development",
            "SECRET_KEY=" + old.get("SECRET_KEY", secrets.token_urlsafe(40)),
            "INTERNAL_API_KEY=" + old.get("INTERNAL_API_KEY", secrets.token_urlsafe(40)),
            "ADMIN_USERNAME=admin",
            "ADMIN_PASSWORD=" + pw,
            "DATABASE_URL=sqlite:///" + db,
            "ENABLE_DEV_TOOLS=false",
            "DRY_RUN=true",
            "APP_TIMEZONE=Asia/Kolkata",
            "CURRENCY_SYMBOL=$",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        created = True
    elif _drop_fake_settings(path):
        say("Purani settings file mein nakli (demo) options band kar diye.")
    return read_env(path), created


def _drop_fake_settings(path: Path) -> bool:
    """Older versions wrote SUPPLIER_BACKEND=mock / STORE_BACKEND=mock / ENABLE_DEV_TOOLS=true (simulated supplier + demo tools). Remove them from an existing file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out, changed = [], False
    for ln in lines:
        key, _, val = ln.partition("=")
        if key.strip() in ("SUPPLIER_BACKEND", "STORE_BACKEND"):
            changed = True
            continue
        if key.strip() == "ENABLE_DEV_TOOLS" and val.strip().lower() in ("1", "true", "yes", "on"):
            out.append("ENABLE_DEV_TOOLS=false")
            changed = True
            continue
        out.append(ln)
    if changed:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


JUNK_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
KEEP_DIRS = {"node_modules", ".git", "venv", ".venv"}


def tidy(root: Path, dd: Path) -> int:
    """Delete only what the app itself generated and can recreate: Python bytecode caches, test/lint caches, the temporary website folders the hosting step
    makes, and rotated log files older than 14 days. Never touches: the database, your env file, keys, the current log, the venv, your code."""
    n = 0
    for base, dirs, _files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in KEEP_DIRS]
        for d in [d for d in dirs if d in JUNK_DIRS]:
            shutil.rmtree(Path(base) / d, ignore_errors=True)
            dirs.remove(d)
            n += 1
    import glob
    import tempfile
    for p in glob.glob(str(Path(tempfile.gettempdir()) / "cheaperdeals_site_*")):
        shutil.rmtree(p, ignore_errors=True)
        n += 1
    cutoff = time.time() - 14 * 86400
    for logs in (root / "api" / "logs", dd / "logs"):
        for f in logs.glob("*.log.*") if logs.exists() else []:
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    n += 1
            except OSError:
                pass
    return n


def goodbye(root: Path, dd: Path) -> None:
    try:
        n = tidy(root, dd)
    except Exception:  # cleaning is a courtesy: it must never block closing
        n = 0
    say("")
    say("=" * 60)
    say(f"  Safai ho gayi: {n} faltu cheezein hata di (cache/temp)." if n else "  Safai ho gayi: kuch faltu bacha hi nahi.")
    say("  Tumhara data, keys aur logs safe hain.")
    say("  Goodbye! Phir milte hain. Bye bye!")
    say("=" * 60)


def free_port(start: int = 8000) -> int:
    for port in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    fail("No free port between 8000 and 8019. Close other programs and try again.")


def main() -> None:
    args = set(sys.argv[1:])
    if sys.version_info < (3, 11):
        fail(f"Python 3.11 or newer is needed (you have {sys.version.split()[0]}). Install it from python.org and tick 'Add Python to PATH'.")
    if not (API / "app" / "main.py").exists():
        fail(f"Cannot find api\\app\\main.py under {ROOT}. Run this from the cheapedeal folder.")

    dd = data_dir()
    py = ensure_venv(dd)
    env_vars, created = ensure_env(dd, "--reset-password" in args)
    local = ROOT / "local.env"  # optional file you own: extra settings such as CJ_API_KEY, kept next to run_local.bat
    if local.exists():
        env_vars.update({k: v for k, v in read_env(local).items() if v})
    say("[3/4] Settings ready.")
    if "--setup-only" in args:
        say("Setup done.")
        return

    if "--disable-2fa" in args:  # lost your phone? this turns 2FA off in the database, then you can log in with the password
        import sqlite3
        db_url = env_vars.get("DATABASE_URL", "")
        path = db_url.replace("sqlite:///", "", 1)
        con = sqlite3.connect(path)
        con.execute("UPDATE settings SET value='false' WHERE key='totp_enabled'")
        con.commit()
        con.close()
        say("2FA is OFF now. Start the app normally and log in with your password.")
        return

    port = int(os.environ.get("PORT") or os.environ.get("CHEAPEDEAL_PORT") or 0) or free_port()
    (ROOT / "connectors").mkdir(exist_ok=True)
    env = {**os.environ, **env_vars, "CHEAPEDEAL_NO_DOTENV": "1", "PYTHONUNBUFFERED": "1", "PUBLIC_URL": os.environ.get("CHEAPEDEAL_PUBLIC_URL") or f"http://localhost:{port}",
           "INTERNAL_SCHEDULER": "true",  # the agents run inside the app: no n8n needed on your PC
           "ALLOW_CONNECTOR_UPLOAD": "true", "CONNECTORS_DIR": str(ROOT / "connectors")}
    url = env["PUBLIC_URL"]
    host = os.environ.get("CHEAPEDEAL_HOST", "127.0.0.1")  # on a VPS set 0.0.0.0 and put HTTPS (Caddy / Cloudflare Tunnel) in front
    say("[4/4] Starting Cheape Deal...")
    proc = subprocess.Popen([str(py), "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port),
                             "--log-level", "warning"], cwd=str(API), env=env)
    try:
        for _ in range(60):
            if proc.poll() is not None:
                fail("The server stopped during start-up. The message above says why.")
            try:
                if urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).status == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            fail("The server did not become ready in 30 seconds.")
        say("")
        say("=" * 60)
        say(f"  Cheape Deal is running:  {url}")
        say(f"  Username: {env_vars['ADMIN_USERNAME']}")
        say(f"  Password: {env_vars['ADMIN_PASSWORD']}")
        say("  (asli CJ se kaam; Rehearsal mode ON jab tak Live na karo)")
        say("  Press Ctrl+C in this window to stop.")
        say("=" * 60)
        if created:
            say("  Pehli baar? API Keys page par CJ key daalo, phir upar Start dabao: baaki sab khud hoga.")
        if not os.environ.get("CHEAPEDEAL_NO_BROWSER"):
            webbrowser.open(url)
        proc.wait()
    except KeyboardInterrupt:
        say("\nBand kar raha hun... (kaam ruk raha hai, thoda ruko)")
        try:  # Ctrl+C reached the server too: let it finish its own shutdown (flush logs, stop agents) before we step in
            proc.wait(timeout=20)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        goodbye(ROOT, data_dir())


if __name__ == "__main__":
    main()
