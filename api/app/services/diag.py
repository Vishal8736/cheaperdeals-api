"""Logs you can actually debug with: saved in the database (so they survive restarts, also on Render), secrets removed, the same
problem grouped under one fingerprint, a plain-Hinglish hint for known problems, and a one-click report to send to whoever helps you.

Parts:  redact()  -> hides keys/passwords/tokens in any text
        install() -> attaches the DB + file handlers to Python's logging and catches crashes nobody handled
        hint_for()-> "what does this error mean and what do I do" for the problems seen so far
        recent()/groups()/summary()/report_text() -> what the Diagnostics page shows"""
from __future__ import annotations

import atexit
import hashlib
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import time
import traceback
from collections import deque
from datetime import timedelta
from pathlib import Path

from ..db import utcnow
from ..models import DiagEvent

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
CATEGORIES = ("web", "cli", "launch", "engine", "supplier", "payment", "email", "scheduler", "security", "app")
MAX_MSG, MAX_TRACE = 2000, 6000
KEEP_DAYS, KEEP_ROWS = 14, 20000
OWN_LOGGERS = ("cheapedeal", "app", "uvicorn", "tests")
NOISY = ("sqlalchemy", "httpx", "httpcore", "watchfiles", "multipart", "asyncio", "urllib3", "PIL", "apscheduler")

# ---- secrets ---------------------------------------------------------------------------------------------------------
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:@/\s]+:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"CJ\w*@api@\w+"), "***"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|rnd_[A-Za-z0-9]{16,}|xox[bap]-[A-Za-z0-9-]{10,})\b"), "***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer ***"),
    (re.compile(r"(?i)((?:--env-var\s+)?[A-Z0-9_]*(?:KEY|SECRET|PASSWORD|PASSWD|TOKEN|DATABASE_URL|CREDENTIAL)[A-Z0-9_]*\s*=\s*)(?:\"[^\"]*\"|'[^']*'|[^\s&]+)"), r"\1***"),
    (re.compile(r"(?i)(\"?(?:authorization|x-internal-key|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret)\"?\s*[:=]\s*\"?)[^\s\"',;&}]{4,}"), r"\1***"),
    (re.compile(r"(?i)([?&](?:key|token|api_key|apikey|password|secret|code)=)[^&\s]+"), r"\1***"),
    (re.compile(r"\b(?:user_code=)[A-Z0-9-]+"), "user_code=***"),
]
_known: list[str] = []  # exact stored secrets (loaded by refresh_secrets), longer than 5 chars


def redact(text: str) -> str:
    text = text if isinstance(text, str) else str(text)
    for v in _known:
        if v in text:
            text = text.replace(v, "***")
    for rx, rep in _PATTERNS:
        text = rx.sub(rep, text)
    return text


def refresh_secrets(db) -> None:
    """Remember the real secret values stored by this app so they are hidden even when they do not look like a key."""
    from . import launch, shopcfg as C
    vals = []
    for k in list(launch.SECRET_KEYS) + ["cj_api_key_enc", "shop_pay_secret_enc", "flipkart_token_enc", "mail_password_enc", "ai_api_key_enc"]:
        try:
            v = C.read_secret(db, k)
        except Exception:
            v = ""
        if v and len(v) > 5:
            vals.append(v)
    _known[:] = vals


# ---- fingerprint, category, hints -------------------------------------------------------------------------------------
def _normalise(msg: str) -> str:
    msg = re.sub(r"https?://\S+", "<url>", msg)
    msg = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", msg)
    msg = re.sub(r"(['\"]).*?\1", "<s>", msg)
    msg = re.sub(r"[A-Za-z]:\\[^\s]+|/[\w./-]{3,}", "<path>", msg)
    msg = re.sub(r"\d+", "#", msg)
    return msg[:200]


def fingerprint(category: str, exc_type: str, message: str, trace: str = "") -> str:
    frame = ""
    if trace:
        m = re.findall(r'File "([^"]+)", line \d+, in (\w+)', trace)
        if m:
            frame = f"{Path(m[-1][0]).name}:{m[-1][1]}"
    base = f"{category}|{exc_type}|{frame}|{_normalise(message.splitlines()[0] if message else '')}"
    return hashlib.sha1(base.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:12]


def category_for(name: str, explicit: str | None = None) -> str:
    if explicit in CATEGORIES:
        return explicit
    n = name or ""
    for key, cat in (("hostcli", "cli"), (".cli", "cli"), ("launch", "launch"), ("gitpush", "launch"), ("hosting", "launch"), ("engine", "engine"), ("agents", "engine"),
                     ("orders", "engine"), ("connectors", "supplier"), ("sync", "supplier"), ("pricefeed", "supplier"), ("payments", "payment"), ("shop", "payment"),
                     ("mailer", "email"), ("notify", "email"), ("scheduler", "scheduler"), ("job", "scheduler"), ("keepalive", "scheduler"), ("security", "security"), ("auth", "security")):
        if key in n:
            return cat
    return "app"


HINTS: list[tuple[str, str, str]] = [
    (r"yaml: unmarshal|cannot unmarshal", "Render CLI ki config file kharab (cli.yaml)",
     "App ise khud theek karti hai (Status check ya Start dabao). Na ho to ~/.render/cli.yaml (Windows: C:\\Users\\<naam>\\.render\\cli.yaml) kholo, 'workspace:' wali lines hata do ya file delete karke 'render login' dobara karo."),
    (r"no workspace set", "Render workspace set nahi hai",
     "Render CLI pehle workspace maangta hai. App ek workspace ho to khud set karti hai; warna terminal mein ek baar `render workspace set` chalao, phir Start dabao."),
    (r"WinError 2|FileNotFoundError.*(vercel|npx|render|gh|neon|node)|CLI.*(nahi mili|chal nahi payi)", "CLI tool computer par nahi mili",
     "Node.js (vercel/neon ke liye), `render` ya `gh` PATH mein nahi hai. Install karo, naya terminal kholo, phir app dobara chalao (API Keys page par install ka tareeka likha hai)."),
    (r"ETARGET|notarget|No matching version found", "npm ko CLI ka version/dependency nahi mili",
     "Neon ke liye terminal mein `npm i -g neonctl@7.0.8` chalao, phir `neon login`. Net/npm registry ya antivirus bhi check karo."),
    (r"not logged in|not authenticated|login nahi|Not authorized|please login|unauthenticated", "CLI mein login nahi hai",
     "API Keys page par us provider ka 'login (CLI)' dabao, browser mein allow karo, phir 'Status check'."),
    (r"\b401\b|Unauthorized|Invalid (api )?key|CJ.*(token|key).*(invalid|expired)", "Key ya login galat/expire",
     "CJ ke liye dashboard se nayi API key banao aur API Keys page par daalo. Doosre provider ke liye us CLI se dobara login karo."),
    (r"\b429\b|Too Many Requests|rate limit", "Rate limit: bahut jaldi-jaldi request",
     "Thodi der ruko (CJ ~1 request/sec, GitHub kuch minute). App khud retry karti hai; baar-baar aaye to agents ka interval badhao."),
    (r"timed? ?out|Timeout|timeout", "Jawab nahi aaya (timeout)",
     "Net slow hai, ya Render free server so raha hai (pehli request 30-60 sec leti hai). Thodi der baad dobara try karo; keep-alive ping ON rakho."),
    (r"database is locked", "Database lock (SQLite)",
     "Do copy ek hi database par chal rahi hain. Doosri window/process band karo ya sirf ek run_local.bat chalao."),
    (r"UNIQUE constraint failed|duplicate key", "Same record dobara banane ki koshish",
     "Ye cheez pehle se maujood hai. Aam taur par harmless; baar-baar aaye to batao kaunsa record hai (Error ID bhejo)."),
    (r"ModuleNotFoundError|No module named", "Python package install nahi hai",
     "venv activate karke chalao: `pip install -r api/requirements.txt`, phir app dobara start karo."),
    (r"Connection refused|ConnectError|Name or service not known|getaddrinfo failed|Temporary failure in name resolution|Network is unreachable", "Server tak pahunch nahi paye",
     "Internet/DNS/VPN dekho. Render server so raha ho to /ping kholo aur 1 minute ruko. Firewall ya antivirus bhi block kar sakta hai."),
    (r"CERTIFICATE_VERIFY_FAILED|SSL.*(error|fail)|certificate", "SSL/certificate error",
     "Computer ki date/time sahi karo, antivirus 'HTTPS scanning' band karke dekho, ya `pip install -U certifi`."),
    (r"Address already in use|Errno 10048|only one usage of each socket", "Port 8000 pehle se use mein hai",
     "Pichla run_local.bat/uvicorn band karo (Task Manager mein python.exe), phir dobara chalao."),
    (r"password authentication failed|DATABASE_URL|OperationalError.*(psycopg|postgres)|could not connect to server", "Database (Neon/Postgres) se connect nahi hua",
     "Database address galat/expire ho sakta hai. Neon login dobara karo; Render par DATABASE_URL dashboard mein check karo. Neon free DB idle par so jata hai: 10 sec baad retry."),
    (r"Deployment Protection|vercel.*\b(401|403)\b|/api.*ka jawab", "Vercel par /api block hai",
     "Vercel project > Settings > Deployment Protection band karo, phir Start dobara dabao."),
    (r"PermissionError|WinError 5|Access is denied", "File/folder permission",
     "Project folder OneDrive/Program Files ke andar ho to Desktop jaise normal folder mein le jao ya admin ke bina likhne layak folder chuno."),
    (r"UnicodeEncodeError|UnicodeDecodeError|charmap", "Hindi/special akshar par Windows encoding galti",
     "CMD mein `set PYTHONUTF8=1` karke app chalao (ya run_local.bat mein ye line jodo)."),
    (r"MemoryError|Out of memory|Killed|R14", "Memory khatam (Render free 512 MB)",
     "Workers 1 rakho, bade imports ek-ek karke karo. Zarurat ho to Render plan upgrade."),
    (r"CSRF|csrf", "Form ka security token purana hai",
     "Page refresh (Ctrl+F5) karke dobara try karo. Baar-baar aaye to logout/login karo."),
    (r"SECRET_KEY|insecure|Refusing to start", "Config unsafe/adhoora",
     "local.env/.env mein SECRET_KEY lamba random daalo aur ADMIN_PASSWORD badlo, phir restart."),
    (r"gh auth|GitHub CLI|GitError|github\.com.*(403|404|422)", "GitHub step fail",
     "'GitHub login (CLI)' dabao ya terminal mein `gh auth login`. Repo naam pehle se kisi aur ka ho to naya naam chuno."),
    (r"/ping ka jawab nahi|backend.*ping", "Render backend jawab nahi de raha",
     "Render dashboard > service > Logs dekho: build fail, galat DATABASE_URL ya start-command ki galti aam wajah hai."),
    (r"CJ|cjdropshipping", "CJ Dropshipping se jude problem",
     "API Keys page par 'CJ test' dabao. Key sahi hai to CJ ka wallet/stock/shipping check karo; error text CJ ka apna message hai."),
]
_HINTS = [(re.compile(p, re.I), t, f) for p, t, f in HINTS]


def hint_for(*texts: str) -> dict | None:
    blob = "\n".join(t for t in texts if t)
    for rx, title, fix in _HINTS:
        if rx.search(blob):
            return {"title": title, "fix": fix}
    return None


# ---- writer ---------------------------------------------------------------------------------------------------------
class Sink:
    """Collects records in memory, a background thread (or flush()) writes them to the database in batches.
    Writing in a separate thread means logging can never slow down or deadlock a request."""

    def __init__(self, database, threaded: bool):
        self.database = database
        self.q: deque[dict] = deque(maxlen=5000)
        self.lock = threading.Lock()
        self.last_error = ""
        self.written = 0
        self.dropped = 0
        self._guard = threading.local()
        self._seen: dict[str, list] = {}  # fingerprint -> [window_start, count, suppressed]
        self._stop = threading.Event()
        self._ticks = 0
        self.thread = None
        if threaded:
            self.thread = threading.Thread(target=self._loop, daemon=True, name="diag-writer")
            self.thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(2.0):
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        self.flush()

    def add(self, rec: dict) -> None:
        now = time.time()
        w = self._seen.setdefault(rec["fingerprint"], [now, 0, 0])
        if now - w[0] > 60:
            if w[2]:
                self.q.append({**rec, "level": "WARNING", "message": f"(ye wahi problem {w[2]} baar aur aayi pichhle 1 minute mein, tang na karne ke liye save nahi ki)", "trace": "", "exc_type": ""})
            w[:] = [now, 0, 0]
        w[1] += 1
        if w[1] > 20 and rec["level"] in ("INFO", "WARNING", "DEBUG"):
            w[2] += 1
            self.dropped += 1
            return
        if w[1] > 60:  # even errors: a crash loop must not fill the database
            w[2] += 1
            self.dropped += 1
            return
        self.q.append(rec)

    def flush(self) -> int:
        if getattr(self._guard, "busy", False):
            return 0
        self._guard.busy = True
        try:
            with self.lock:
                batch = []
                while self.q:
                    batch.append(self.q.popleft())
                if not batch:
                    return 0
                try:
                    with self.database.session() as s:
                        s.add_all([DiagEvent(**b) for b in batch])
                    self.written += len(batch)
                    self.last_error = ""
                    self._ticks += 1
                    if self._ticks % 300 == 1:
                        with self.database.session() as s:
                            prune(s)
                            refresh_secrets(s)
                except Exception as e:  # never let logging break the app
                    self.last_error = f"{type(e).__name__}: {e}"[:200]
                    for b in batch[-200:]:
                        self.q.append(b)
                    return 0
                return len(batch)
        finally:
            self._guard.busy = False


class DbHandler(logging.Handler):
    def __init__(self, sink: Sink):
        super().__init__(logging.INFO)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            name = record.name or ""
            own = name.split(".")[0] in OWN_LOGGERS
            if name.split(".")[0] in NOISY and record.levelno < logging.ERROR:
                return
            if not own and record.levelno < logging.WARNING:
                return
            if name == "uvicorn.access":
                return
            msg = redact(record.getMessage())[:MAX_MSG]
            exc_type, trace = "", ""
            if record.exc_info and record.exc_info[0] is not None:
                exc_type = record.exc_info[0].__name__
                trace = redact("".join(traceback.format_exception(*record.exc_info)))[-MAX_TRACE:]
                if exc_type and exc_type not in msg:
                    msg = f"{msg} [{exc_type}: {redact(str(record.exc_info[1]))[:300]}]"
            cat = category_for(name, getattr(record, "cat", None))
            rec = dict(created_at=utcnow(), level=record.levelname if record.levelname in LEVELS else "INFO", category=cat, logger=name[:80], message=msg,
                       exc_type=exc_type[:120], trace=trace, fingerprint=fingerprint(cat, exc_type, msg, trace),
                       ref=str(getattr(record, "ref", "") or "")[:12], path=str(getattr(record, "path", "") or "")[:200])
            self.sink.add(rec)
            if record.levelno >= logging.ERROR and self.sink.thread is None:
                pass  # test mode: flushed when the Diagnostics page (or a test) asks
        except Exception:
            pass


class _RedactFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


_STATE: dict = {"sink": None, "installed": False, "file": ""}


def sink() -> Sink | None:
    return _STATE["sink"]


def flush() -> None:
    s = _STATE["sink"]
    if s:
        s.flush()


def install(database, app_env: str = "dev", log_dir: str | None = None) -> Sink:
    """Attach to Python logging. Safe to call again (tests build many apps): the previous handler is replaced."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, (DbHandler, logging.handlers.RotatingFileHandler)) and getattr(h, "_cd", False):
            root.removeHandler(h)
            if isinstance(h, DbHandler):
                h.sink.stop()
    threaded = app_env != "test"
    sk = Sink(database, threaded)
    h = DbHandler(sk)
    h._cd = True  # type: ignore[attr-defined]
    root.addHandler(h)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    _STATE["file"] = ""
    if app_env != "test":
        try:
            d = Path(log_dir or os.getenv("LOG_DIR") or Path(__file__).resolve().parents[3] / "logs")
            d.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(d / "cheaperdeals.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(_RedactFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
            fh.setLevel(logging.INFO)
            fh._cd = True  # type: ignore[attr-defined]
            root.addHandler(fh)
            _STATE["file"] = str(d / "cheaperdeals.log")
        except OSError:
            pass
    _STATE["sink"] = sk
    if not _STATE["installed"]:
        _STATE["installed"] = True
        log = logging.getLogger("cheapedeal")
        prev_exc = sys.excepthook

        def _hook(t, v, tb):
            log.critical("Unhandled crash: %s", v, exc_info=(t, v, tb))
            flush()
            prev_exc(t, v, tb)
        sys.excepthook = _hook
        prev_thr = threading.excepthook

        def _thook(args):
            if args.exc_type is not SystemExit:
                log.critical("Thread %s crashed", getattr(args.thread, "name", "?"), exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
            prev_thr(args)
        threading.excepthook = _thook
        atexit.register(flush)
    try:
        with database.session() as s:
            refresh_secrets(s)
    except Exception:
        pass
    return sk


def new_ref() -> str:
    import secrets
    return secrets.token_hex(3).upper()


# ---- reading ---------------------------------------------------------------------------------------------------------
def prune(db, days: int = KEEP_DAYS, max_rows: int = KEEP_ROWS) -> int:
    n = db.query(DiagEvent).filter(DiagEvent.created_at < utcnow() - timedelta(days=days)).delete(synchronize_session=False)
    total = db.query(DiagEvent).count()
    if total > max_rows:
        cutoff = db.query(DiagEvent.id).order_by(DiagEvent.id.desc()).offset(max_rows).limit(1).scalar()
        if cutoff:
            n += db.query(DiagEvent).filter(DiagEvent.id <= cutoff).delete(synchronize_session=False)
    db.commit()
    return n


def _q(db, level=None, category=None, q=None, hours=24, ref=None):
    qs = db.query(DiagEvent)
    if hours:
        qs = qs.filter(DiagEvent.created_at >= utcnow() - timedelta(hours=hours))
    if level and level in LEVELS:
        qs = qs.filter(DiagEvent.level.in_([k for k, v in LEVELS.items() if v >= LEVELS[level]]))
    if category:
        qs = qs.filter(DiagEvent.category == category)
    if ref:
        qs = qs.filter(DiagEvent.ref == ref.strip().upper())
    if q:
        like = f"%{q}%"
        qs = qs.filter(DiagEvent.message.ilike(like) | DiagEvent.trace.ilike(like) | DiagEvent.path.ilike(like) | DiagEvent.ref.ilike(like))
    return qs


def recent(db, level=None, category=None, q=None, hours=24, ref=None, limit=200) -> list[DiagEvent]:
    flush()
    return _q(db, level, category, q, hours, ref).order_by(DiagEvent.id.desc()).limit(limit).all()


def groups(db, level="WARNING", category=None, q=None, hours=24, limit=40) -> list[dict]:
    """The same problem counted once: how many times, first/last time, the newest example, and a hint."""
    flush()
    rows = _q(db, level, category, q, hours).order_by(DiagEvent.id.desc()).limit(3000).all()
    out: dict[str, dict] = {}
    for r in rows:
        g = out.get(r.fingerprint)
        if g is None:
            g = out[r.fingerprint] = {"fingerprint": r.fingerprint, "n": 0, "last": r.created_at, "first": r.created_at, "level": r.level, "category": r.category,
                                      "message": r.message, "exc_type": r.exc_type, "trace": r.trace, "ref": r.ref, "path": r.path,
                                      "hint": hint_for(r.message, r.trace)}
        g["n"] += 1
        g["first"] = r.created_at
        if LEVELS.get(r.level, 0) > LEVELS.get(g["level"], 0):
            g["level"] = r.level
    return sorted(out.values(), key=lambda g: (-LEVELS.get(g["level"], 0), -g["n"]))[:limit]


def summary(db, hours=24) -> dict:
    flush()
    since = utcnow() - timedelta(hours=hours)
    base = db.query(DiagEvent).filter(DiagEvent.created_at >= since)
    err = base.filter(DiagEvent.level.in_(["ERROR", "CRITICAL"])).count()
    warn = base.filter(DiagEvent.level == "WARNING").count()
    cats: dict[str, int] = {}
    for c, n in db.query(DiagEvent.category, DiagEvent.id).filter(DiagEvent.created_at >= since, DiagEvent.level.in_(["WARNING", "ERROR", "CRITICAL"])).all():
        cats[c] = cats.get(c, 0) + 1
    g = groups(db, "ERROR", hours=hours, limit=1)
    return {"errors": err, "warnings": warn, "total": base.count(), "by_category": dict(sorted(cats.items(), key=lambda kv: -kv[1])), "top": g[0] if g else None}


def error_count(db, hours=24) -> int:
    try:
        return db.query(DiagEvent).filter(DiagEvent.created_at >= utcnow() - timedelta(hours=hours), DiagEvent.level.in_(["ERROR", "CRITICAL"])).count()
    except Exception:
        return 0


def health_checks(db, cfg=None) -> list[dict]:
    """Quick yes/no checks, no slow network calls. ok: True / False / None (unknown)."""
    import shutil

    from . import shopcfg as C
    from .launch import state as launch_state
    res = []

    def add(name, ok, detail=""):
        res.append({"name": name, "ok": ok, "detail": detail})
    try:
        db.execute(__import__("sqlalchemy").text("SELECT 1"))
        add("Database", True, "jawab de raha hai")
    except Exception as e:
        add("Database", False, redact(str(e))[:150])
    sk = sink()
    if sk is None:
        add("Log writer", False, "install nahi hua")
    else:
        add("Log writer", not sk.last_error, sk.last_error or f"{sk.written} line save hui" + (f", {sk.dropped} dohrai hui chhodi" if sk.dropped else ""))
    add("Log file", None if not _STATE["file"] else True, _STATE["file"] or "file log band (test/dev)")
    cj = C.read_secret(db, "host_env_cj_enc")
    add("CJ API key", bool(cj), "daali hui hai" if cj else "API Keys page par daalo")
    for w, exe in (("Vercel", "vercel"), ("Render", "render"), ("GitHub", "gh"), ("Neon", "neon")):
        found = shutil.which(exe) or (shutil.which("neonctl") if w == "Neon" else None) or (shutil.which("npx") if w in ("Vercel", "Neon") else None)
        add(f"{w} CLI", bool(found), found or "PATH mein nahi mili")
    st = launch_state(db)
    add("Pichhla Start/hosting", True if st == "ok" else (False if st == "error" else None), {"ok": "poora hua", "error": "fail hua (Website jodo page par log dekho)", "running": "chal raha hai"}.get(st, "abhi chala nahi"))
    e1 = error_count(db, 1)
    add("Pichhle 1 ghante ke errors", e1 == 0, f"{e1} error" if e1 else "koi nahi")
    try:
        from . import keepalive
        last = getattr(keepalive, "LAST", None)
        add("Keep-alive ping", None if not last else bool(last.get("ok", True)) if isinstance(last, dict) else None, str(last)[:120] if last else "abhi chala nahi / band")
    except Exception:
        pass
    return res


def report_text(db, cfg=None, hours=24) -> str:
    """Everything needed to understand a problem, in one text file with all secrets removed."""
    from . import launch
    flush()
    L = [f"CHEAPERDEALS DIAGNOSTIC REPORT  {utcnow():%Y-%m-%d %H:%M} UTC  (last {hours}h)",
         f"Python {platform.python_version()} on {platform.platform()}",
         f"APP_ENV={getattr(cfg, 'app_env', '?')}  RENDER={'yes' if os.getenv('RENDER') else 'no'}", ""]
    L.append("== HEALTH ==")
    for h in health_checks(db, cfg):
        L.append(f"[{'OK ' if h['ok'] else ('?? ' if h['ok'] is None else 'BAD')}] {h['name']}: {h['detail']}")
    L += ["", "== HOSTING (Start) LOG =="]
    for lv, msg in launch.log_of(db):
        L.append(f"[{lv}] {msg}")
    L += ["", "== PROBLEMS (grouped) =="]
    gs = groups(db, "WARNING", hours=hours, limit=30)
    if not gs:
        L.append("none")
    for g in gs:
        L.append(f"- x{g['n']} [{g['level']}/{g['category']}] last {g['last']:%H:%M:%S} id={g['fingerprint']} ref={g['ref'] or '-'}")
        L.append(f"    {g['message'][:400]}")
        if g["hint"]:
            L.append(f"    HINT: {g['hint']['title']} -> {g['hint']['fix']}")
        if g["trace"]:
            L.append("    " + "\n    ".join(g["trace"].strip().splitlines()[-12:]))
    L += ["", "== LAST 300 LOG LINES (newest first) =="]
    for r in recent(db, hours=hours, limit=300):
        L.append(f"{r.created_at:%m-%d %H:%M:%S} {r.level:<8} {r.category:<9} {r.ref or '':<6} {r.message[:300]}")
    return redact("\n".join(L))


def log_cli(cmd: list[str], code: int, secs: float, out: str = "", err: str = "") -> None:
    """One line per CLI command (vercel/render/gh/neon): what ran, exit code, how long, and the tail of its complaint when it failed."""
    lg = logging.getLogger("cheapedeal.cli")
    name = " ".join(Path(cmd[0]).stem.lower() if i == 0 else c for i, c in enumerate(cmd[:5]))
    if code == 0:
        lg.info("cli ok (%.1fs): %s", secs, name, extra={"cat": "cli"})
    else:
        tail = ((err or "").strip() or (out or "").strip())[-400:]
        lg.warning("cli fail exit %s (%.1fs): %s | %s", code, secs, name, tail, extra={"cat": "cli"})
