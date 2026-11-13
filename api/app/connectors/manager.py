"""ConnectorManager: turns saved Connection rows into live adapters, tests them, and picks which one to use."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..adapters.base import StoreError, SupplierError
from ..db import Database, utcnow
from ..models import Connection
from ..secretbox import seal, unseal
from ..services import settings_service as S
from .registry import BUILTIN, ConnectorType, load_connector_files

log = logging.getLogger("cheapedeal.connectors")



def scrub(msg: str) -> str:
    """Error text shown in the UI: no SQL dump, no tokens or keys."""
    msg = msg.split(" [SQL:")[0].split("(Background on this error")[0]
    msg = re.sub(r"eyJ[\w.\-]{20,}", "***", msg)
    return re.sub(r"(?:API@)?CJ\d+@(?:api@|CJ:)?[\w.\-@]{8,}", "***", msg)[:600]


class ConnectorManager:
    def __init__(self, database: Database, secret_key: str, folder: Path):
        self.database = database
        self.secret_key = secret_key
        self.folder = Path(folder)
        self._types: dict[str, ConnectorType] = {}
        self.file_errors: dict[str, str] = {}
        self._cache: dict[int, tuple[tuple, Any]] = {}
        self.reload()

    # ---- types -----------------------------------------------------------------------------
    def reload(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        types = {t.key: t for t in BUILTIN}
        file_types, self.file_errors = load_connector_files(self.folder)
        for t in file_types:
            types[t.key] = t
        self._types = types
        self._cache.clear()

    def types(self, kind: str | None = None) -> list[ConnectorType]:
        return [t for t in self._types.values() if kind in (None, t.kind)]

    def type(self, key: str) -> ConnectorType | None:
        return self._types.get(key)

    # ---- connections -----------------------------------------------------------------------
    def split_form(self, ctype: ConnectorType, form: dict, existing: Connection | None = None) -> tuple[dict, dict]:
        """Form values -> (public config, secrets). A blank secret field keeps the stored secret."""
        old = unseal(self.secret_key, existing.secrets_enc) if existing else {}
        cfg, sec = {}, {}
        for f in ctype.fields:
            v = (form.get(f.key) or "").strip()
            if f.secret:
                sec[f.key] = v or old.get(f.key, "")
            else:
                cfg[f.key] = v
        return cfg, sec

    def missing(self, ctype: ConnectorType, cfg: dict, sec: dict) -> list[str]:
        return [f.label for f in ctype.fields if f.required and not (sec.get(f.key) if f.secret else cfg.get(f.key))]

    def save(self, db, *, conn: Connection | None, kind: str, type_key: str, name: str, cfg: dict, sec: dict) -> Connection:
        if conn is None:
            conn = Connection(kind=kind, type=type_key)
            db.add(conn)
        conn.name = name.strip()[:120] or type_key
        conn.config_json = json.dumps(cfg)
        conn.secrets_enc = seal(self.secret_key, sec)
        conn.last_status, conn.last_error = "untested", ""
        db.flush()
        self._cache.pop(conn.id, None)
        return conn

    def token_key(self, conn: Connection) -> str:
        return f"cj_tokens:{conn.id}"

    def read_tokens(self, db, conn: Connection) -> dict:
        raw = S.get(db, self.token_key(conn), "")
        return json.loads(raw) if raw else {}

    def write_tokens(self, db, conn: Connection, t: dict) -> None:
        S.set_value(db, self.token_key(conn), json.dumps(t))
        self._cache.pop(conn.id, None)

    def secrets_of(self, conn: Connection) -> dict:
        return unseal(self.secret_key, conn.secrets_enc)

    def secrets_unreadable(self, conn: Connection) -> bool:
        return bool(conn.secrets_enc) and not self.secrets_of(conn)

    def build(self, conn: Connection, db=None):
        """db: when given (a connection test inside a web request) tokens are stored through THAT session. A second session would wait for
        the write lock the request already holds and SQLite answers 'database is locked'. A db-bound adapter is never cached."""
        ctype = self._types.get(conn.type)
        if ctype is None:
            raise RuntimeError(f"Connector type '{conn.type}' is not installed (was its file removed?)")
        fp = (conn.type, conn.config_json, conn.secrets_enc)
        hit = self._cache.get(conn.id) if db is None else None
        if hit and hit[0] == fp:
            return hit[1]
        ctx = {}
        if conn.type == "cj_dropshipping":
            key = f"cj_tokens:{conn.id}"

            def load() -> dict:
                if db is not None:
                    raw = S.get(db, key, "")
                else:
                    with self.database.session() as s:
                        raw = S.get(s, key, "")
                return json.loads(raw) if raw else {}

            def save(t: dict) -> None:
                if db is not None:
                    S.set_value(db, key, json.dumps(t))  # the caller commits
                    return
                with self.database.session() as s:
                    S.set_value(s, key, json.dumps(t))

            ctx = {"token_load": load, "token_save": save}
        adapter = ctype.factory(json.loads(conn.config_json or "{}"), self.secrets_of(conn), ctx)
        if not getattr(adapter, "name", None):
            adapter.name = conn.type
        if db is None:
            self._cache[conn.id] = (fp, adapter)
        return adapter

    def test(self, db, conn: Connection) -> tuple[bool, str]:
        try:
            if self.secrets_unreadable(conn):
                raise RuntimeError("Saved keys cannot be read (SECRET_KEY changed). Open Edit and enter them again.")
            adapter = self.build(conn, db)
            fn = getattr(adapter, "test_connection", None)
            msg = fn() if fn else "Saved. This connector has no built-in test."
            conn.last_status, conn.last_error = "ok", ""
            ok = True
        except Exception as e:
            msg = scrub(str(e) or type(e).__name__)
            conn.last_status, conn.last_error = "error", msg[:500]
            ok = False
        conn.last_tested_at = utcnow()
        db.flush()
        return ok, msg

    # ---- picking the adapter for an order / product ----------------------------------------
    def _row(self, cid: int, kind: str) -> Connection | None:
        with self.database.session() as s:
            c = s.get(Connection, cid)
            if c is None or c.kind != kind:
                return None
            s.expunge(c)
            return c

    def _default_row(self, kind: str) -> Connection | None:
        with self.database.session() as s:
            c = s.query(Connection).filter(Connection.kind == kind, Connection.is_default.is_(True), Connection.enabled.is_(True)).first()
            if c:
                s.expunge(c)
            return c

    def supplier(self, cid: int | None, fallback):
        c = self._row(cid, "supplier") if cid else self._default_row("supplier")
        if cid and (c is None or not c.enabled):
            raise SupplierError("The supplier connection for this product was removed or switched off")
        return self.build(c) if c else fallback

    def store(self, cid: int | None, fallback):
        c = self._row(cid, "store") if cid else self._default_row("store")
        if cid and (c is None or not c.enabled):
            raise StoreError("The store connection for this order was removed or switched off")
        return self.build(c) if c else fallback
