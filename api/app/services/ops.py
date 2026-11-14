"""Housekeeping that makes the system run on its own: daily report, backups, health checks, API key, setup checklist."""
from __future__ import annotations

import secrets
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Alert, Connection, FxRate, Market, MarketPrice, Order, Product, StrategyRec
from . import settings_service as S
from .agents import event as agent_event

VERSION = "3.3"
KEEP_BACKUPS = 7


# ---- api key -------------------------------------------------------------------------------
def api_key(db: Session, rotate: bool = False) -> str:
    k = S.get(db, "api_key", "")
    if rotate or not k:
        k = "cd_" + secrets.token_urlsafe(24)
        S.set_value(db, "api_key", k)
    return k


def check_api_key(db: Session, supplied: str) -> bool:
    k = S.get(db, "api_key", "")
    return bool(k) and bool(supplied) and secrets.compare_digest(k, supplied)


# ---- backups -------------------------------------------------------------------------------
def sqlite_path(cfg) -> Path | None:
    url = cfg.database_url
    if not url.startswith("sqlite"):
        return None
    raw = url.split("///", 1)[1] if "///" in url else ""
    return Path(raw).resolve() if raw and raw != ":memory:" else None


def backup_dir(cfg) -> Path | None:
    p = sqlite_path(cfg)
    return (p.parent / "backups") if p else None


def backup_now(cfg) -> Path | None:
    """A consistent copy of the SQLite database (safe while the app is running). Keeps the newest 7. None on Postgres (use pg_dump)."""
    src, dest_dir = sqlite_path(cfg), backup_dir(cfg)
    if src is None or dest_dir is None or not src.exists():
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"cheapedeal-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    a = sqlite3.connect(str(src))
    b = sqlite3.connect(str(dest))
    try:
        a.backup(b)
    finally:
        b.close()
        a.close()
    for old in sorted(dest_dir.glob("cheapedeal-*.db"))[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def list_backups(cfg) -> list[Path]:
    d = backup_dir(cfg)
    return sorted(d.glob("cheapedeal-*.db"), reverse=True) if d and d.is_dir() else []


# ---- daily report --------------------------------------------------------------------------
def today_local(cfg) -> str:
    try:
        return datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


def digest_text(db: Session, cfg) -> str:
    from . import analytics as AN
    r = AN.report(db, cfg, 1)
    failed = db.query(Order).filter(Order.status.in_(["failed_manual", "failed_retry"])).count()
    approvals = db.query(Order).filter(Order.status == "pending_approval").count()
    good = db.query(StrategyRec).filter(StrategyRec.status == "viable").count()
    alerts = db.query(Alert).filter(Alert.resolved.is_(False)).count()
    lines = [f"Cheape Deal daily report: {r['orders']} order(s), revenue ${r['revenue']}, estimated profit ${r['profit']}.",
             f"Needs you: {failed} failed order(s), {approvals} waiting for approval, {alerts} open alert(s).",
             f"Opportunities: {good} price(s) ready to apply."]
    dead = AN.dead_stock(db)
    if dead:
        lines.append(f"{len(dead)} listed product(s) have not sold in 60 days.")
    return "\n".join(lines)


def maintenance(db: Session, rt, cfg) -> dict:
    """Runs hourly. Once per local day: send the report and take a backup. Always: trim old agent events."""
    from . import agents as A
    out = {"report": False, "backup": "", "pruned": False}
    day = today_local(cfg)
    hour = 24
    try:
        hour = datetime.now(ZoneInfo(cfg.timezone)).hour
    except Exception:
        pass
    if S.get(db, "last_digest_day", "") != day and hour >= int(S.get(db, "digest_hour", "9") or 9):
        text = digest_text(db, cfg)
        sent = rt.notifier.send(text)
        S.set_value(db, "last_digest_day", day)
        agent_event(db, "reporter", text.splitlines()[0] + (" Sent to Telegram." if sent else " (Telegram is not set up: shown here only.)"), "ok")
        out["report"] = True
    if S.get(db, "last_backup_day", "") != day:
        try:
            p = backup_now(cfg)
            S.set_value(db, "last_backup_day", day)
            if p is not None:
                out["backup"] = p.name
                agent_event(db, "reporter", f"Backup saved: {p.name}", "ok")
        except Exception as e:
            agent_event(db, "reporter", f"Backup failed: {e}", "error")
            rt.alert(db, level="warning", source="backup", dedupe_key=f"bk-{day}", message=f"The daily database backup failed: {e}")
    A.prune(db)
    out["pruned"] = True
    return out


# ---- heartbeat -----------------------------------------------------------------------------
PING = None  # tests replace this: PING(url)


def heartbeat(db: Session, cfg) -> dict:
    """Ping an uptime-monitor URL (UptimeRobot, healthchecks.io, Better Stack...) every few minutes. If this stops, THEY alert you
    that the whole server is down - something the app cannot tell you itself. Only pings while the scheduler is actually running."""
    url = S.get(db, "heartbeat_url", "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"pinged": False}
    try:
        if PING is not None:
            PING(url)
        else:
            import urllib.request
            urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "cheapedeal-heartbeat"}), timeout=10).read(64)
        S.set_value(db, "heartbeat_last_ok", utcnow().isoformat())
        return {"pinged": True}
    except Exception as e:
        S.set_value(db, "heartbeat_last_error", str(e)[:200])
        return {"pinged": False, "error": str(e)}


# ---- health --------------------------------------------------------------------------------
def health_checks(db: Session, cfg, scheduler_alive: bool | None) -> list[dict]:
    """Plain-language list of what is fine and what is not. status: ok | warn | bad."""
    out: list[dict] = []

    def add(name, status, detail, fix=""):
        out.append({"name": name, "status": status, "detail": detail, "fix": fix})
    add("Rehearsal mode", "warn" if S.get_bool(db, "dry_run") else "ok",
        "Orders are NOT sent to suppliers (rehearsal)." if S.get_bool(db, "dry_run") else "Live: orders are sent to suppliers.",
        "Turn off DRY-RUN in Settings when you are ready." if S.get_bool(db, "dry_run") else "")
    add("Automation", "bad" if S.get_bool(db, "paused") else "ok", "Paused by the kill switch." if S.get_bool(db, "paused") else "Running.", "Resume it in Settings." if S.get_bool(db, "paused") else "")
    if scheduler_alive is None:
        add("Scheduler", "ok", "Handled outside the app (n8n).")
    else:
        add("Scheduler", "ok" if scheduler_alive else "bad", "Built-in scheduler is running." if scheduler_alive else "The built-in scheduler is not running: agents will not run by themselves.", "" if scheduler_alive else "Restart the app.")
    add("Telegram alerts", "ok" if (cfg.telegram_bot_token and cfg.telegram_chat_id) else "warn",
        "Connected." if cfg.telegram_bot_token else "Not set up: alerts only show inside the app.", "" if cfg.telegram_bot_token else "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to the settings file.")
    unconfirmed = [r.code for r in db.query(FxRate).filter(FxRate.source == "seed") if r.code != "USD" and db.query(Market).filter(Market.currency == r.code, Market.enabled.is_(True)).count()]
    add("Exchange rates", "warn" if unconfirmed else "ok", f"Only estimates for: {', '.join(unconfirmed)}." if unconfirmed else "Confirmed.", "Strategy > Refresh from the internet." if unconfirmed else "")
    p = sqlite_path(cfg)
    if p is not None:
        bk = list_backups(cfg)
        add("Backups", "ok" if bk else "warn", f"Latest: {bk[0].name}" if bk else "No backup yet (the first runs within the hour).", "")
        try:
            free = shutil.disk_usage(str(p.parent)).free
            add("Disk space", "ok" if free > 500_000_000 else "bad", f"{free // 1_000_000} MB free.", "" if free > 500_000_000 else "Free some space.")
        except OSError:
            pass
    else:
        add("Backups", "warn", "PostgreSQL: use pg_dump (see docs/DEPLOY.md).")
    return out


def checklist(db: Session) -> list[dict]:
    """Setup progress for the dashboard: each step is done or points to where to do it."""
    from . import mode as MODE
    if MODE.is_affiliate(db):
        progs = db.query(Connection).filter(Connection.kind == "affiliate", Connection.enabled.is_(True)).all()
        return [{"label": a, "done": b, "href": c} for a, b, c in [
            ("Affiliate program jodo (Awin, CJ, impact, Amazon ...)", any(c.last_status == "ok" for c in progs), "/programs"),
            ("Partner products website par aaye", db.query(Product).filter(Product.kind == "affiliate", Product.listing_status == "listed").count() > 0, "/programs"),
            ("Network ki earnings API jodo (asli kamai dikhane ke liye)", any(c.type in ("aff_awin", "aff_impact", "aff_cj_affiliate") and c.secrets_enc for c in progs), "/programs"),
            ("Dukaan online (Vercel + Render)", S.get_bool(db, "host_use_cli") or bool(S.get(db, "shop_site_url", "")), "/keys"),
        ]]
    suppliers = db.query(Connection).filter(Connection.kind == "supplier", Connection.type != "mock_supplier").count()
    stores = db.query(Connection).filter(Connection.kind == "store", Connection.type != "mock_store").count()
    steps = [("Connect CJ Dropshipping", suppliers > 0, "/connect"), ("Connect your store or website", stores > 0, "/connect"),
             ("Confirm exchange rates", db.query(FxRate).filter(FxRate.source != "seed").count() > 1, "/strategy"),
             ("Add your first product", db.query(Product).count() > 0, "/catalog"),
             ("Add a price from another country", db.query(MarketPrice).count() > 0, "/strategy"),
             ("Turn off rehearsal mode (go live)", not S.get_bool(db, "dry_run"), "/settings")]
    return [{"label": a, "done": b, "href": c} for a, b, c in steps]
