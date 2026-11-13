"""Start-up self-configuration: connect CJ Dropshipping by itself when CJ_API_KEY is set (so it survives restarts, no clicking)."""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from ..models import Connection

log = logging.getLogger("cheapedeal.bootstrap")


def ensure_cj(db: Session, mgr, api_key: str) -> str:
    """Create or update the CJ connection from CJ_API_KEY and make it the default supplier. Returns a short status. Never raises."""
    key = (api_key or "").strip()
    if not key:
        return "no CJ_API_KEY"
    try:
        from . import settings_service as S
        if S.get(db, "cj_key_source", "") == "ui":  # a key saved on the API Keys page is permanent: the env file never overrides it
            return "kept (key saved in the app)"
        from . import shopcfg as _C
        _C.save_secret(db, "host_env_cj_enc", key)  # the launcher sends it to the Render server
        ctype = mgr.type("cj_dropshipping")
        conn = db.query(Connection).filter(Connection.type == "cj_dropshipping").order_by(Connection.id).first()
        old_cfg = json.loads(conn.config_json or "{}") if conn else {}
        if conn and mgr.secrets_of(conn).get("api_key") == key and conn.enabled:
            return "already connected"
        form = {**{f.key: (f.default or "") for f in ctype.fields}, **{k: v for k, v in old_cfg.items() if v}, "api_key": key}
        cfg, sec = mgr.split_form(ctype, form, conn)
        conn = mgr.save(db, conn=conn, kind="supplier", type_key="cj_dropshipping", name=(conn.name if conn else "CJ Dropshipping"), cfg=cfg, sec=sec)
        conn.enabled = True
        if not db.query(Connection).filter(Connection.kind == "supplier", Connection.is_default.is_(True), Connection.id != conn.id, Connection.enabled.is_(True)).first():
            conn.is_default = True
        db.commit()
        return "connected from CJ_API_KEY"
    except Exception as e:  # a start-up helper must never stop the app
        db.rollback()
        log.warning("CJ auto-connect failed: %s", e)
        return f"failed: {e}"


def disable_removed_connections(db: Session, mgr) -> int:
    """Connections whose type no longer exists (e.g. the removed AliExpress) are switched off and un-defaulted; their orders are held, never faked."""
    n = 0
    for c in db.query(Connection).all():
        if mgr.type(c.type) is None and (c.enabled or c.is_default):
            c.enabled, c.is_default = False, False
            c.last_status, c.last_error = "error", f"Connector type '{c.type}' was removed from Cheape Deal. Delete this connection."
            n += 1
    if n:
        db.commit()
    return n


def set_cj_key(db: Session, mgr, api_key: str) -> tuple[bool, str]:
    """Save the CJ key from the app (sealed), make CJ the default supplier and test it. The key then survives every restart; the env file no longer overrides it."""
    import json as _json
    from . import settings_service as S
    key = (api_key or "").strip()
    ctype = mgr.type("cj_dropshipping")
    conn = db.query(Connection).filter(Connection.type == "cj_dropshipping").order_by(Connection.id).first()
    form = {**{f.key: (f.default or "") for f in ctype.fields}, "api_key": key}
    if conn:
        old = _json.loads(conn.config_json or "{}")
        form.update({k: old[k] for k in ("from_country", "ship_to_country", "test_product_id") if old.get(k)})
    cfg, sec = mgr.split_form(ctype, form, conn)
    if mgr.missing(ctype, cfg, sec):
        return False, "CJ API key paste karo."
    conn = mgr.save(db, conn=conn, kind="supplier", type_key="cj_dropshipping", name=(conn.name if conn else "CJ Dropshipping"), cfg=cfg, sec=sec)
    conn.enabled = True
    db.query(Connection).filter(Connection.kind == "supplier").update({"is_default": False})
    conn.is_default = True
    S.set_value(db, "cj_key_source", "ui")
    from . import shopcfg as _C
    _C.save_secret(db, "host_env_cj_enc", key)  # the launcher sends it to the Render server
    ok, msg = mgr.test(db, conn)
    db.commit()
    return ok, msg
