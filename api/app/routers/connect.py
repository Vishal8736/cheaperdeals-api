"""Connect page: add / test / switch off supplier and store connections, and manage connector files."""
from __future__ import annotations

import ast
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from ..models import Connection, Order, Product, ProductListing
from ..security import require_login, verify_csrf
from ..services import settings_service as S
from .deps import get_db
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])
EXAMPLES = Path(__file__).resolve().parents[3] / "connectors" / "examples"


def _mgr(request: Request):
    return request.app.state.connectors


@router.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request, db: Session = Depends(get_db)):
    m = _mgr(request)
    rows = db.query(Connection).order_by(Connection.kind, Connection.id).all()
    usage = {}
    for c in rows:
        usage[c.id] = (db.query(Product).filter(Product.supplier_connection_id == c.id).count() if c.kind in ("supplier", "affiliate")
                       else db.query(ProductListing).filter(ProductListing.connection_id == c.id, ProductListing.status == "listed").count())
    cfg = request.app.state.cfg
    base = cfg.public_url.rstrip("/")
    return page(request, db, "connect.html", conns=rows, usage=usage, mgr=m, types_supplier=m.types("supplier"), types_store=m.types("store"),
                file_errors=m.file_errors, folder=str(m.folder), can_upload=cfg.allow_connector_upload, base=base,
                feed_url=f"{base}/feed/products.json?token={S.get(db, 'feed_token')}",
                builtin_supplier=request.app.state.rt.supplier.name, builtin_store=request.app.state.rt.store.name,
                bad_secrets={c.id for c in rows if m.secrets_unreadable(c)})


@router.get("/connect/new", response_class=HTMLResponse)
def connect_new(request: Request, type: str, db: Session = Depends(get_db)):
    t = _mgr(request).type(type)
    if t is None:
        raise HTTPException(404, "Unknown connector type")
    return page(request, db, "connect_form.html", t=t, conn=None, values={}, has_secret={}, config={})


def _form_dict(form) -> dict:
    return {k: v for k, v in form.items() if isinstance(v, str)}


def _validate(m, t, cfg: dict, sec: dict) -> str | None:
    miss = m.missing(t, cfg, sec)
    if miss:
        return "Please fill: " + ", ".join(miss)
    if "spec" in cfg:
        from ..connectors.generic_rest import parse_spec
        try:
            parse_spec(cfg.get("spec", ""))
        except ValueError as e:
            return str(e)
    for k in ("base_url",):
        if k in cfg and cfg[k] and not re.match(r"^https?://", cfg[k]):
            return "Website / API address must start with http:// or https://"
    return None


@router.post("/connect/new", dependencies=[Depends(verify_csrf)])
async def connect_create(request: Request, db: Session = Depends(get_db)):
    m = _mgr(request)
    form = _form_dict(await request.form())
    t = m.type(form.get("type", ""))
    if t is None:
        raise HTTPException(404, "Unknown connector type")
    cfg, sec = m.split_form(t, form)
    err = _validate(m, t, cfg, sec)
    if err:
        flash(request, "error", err)
        return page(request, db, "connect_form.html", t=t, conn=None, values={**cfg, "name": form.get("name", "")}, has_secret={}, config=cfg)
    conn = m.save(db, conn=None, kind=t.kind, type_key=t.key, name=form.get("name") or t.label, cfg=cfg, sec=sec)
    ok, msg = m.test(db, conn)
    db.commit()
    flash(request, "ok" if ok else "warn", f"Saved '{conn.name}'. " + ("Connection works: " if ok else "Saved, but the test failed: ") + msg)
    return back("/connect")


@router.get("/connect/{cid:int}/edit", response_class=HTMLResponse)
def connect_edit(cid: int, request: Request, db: Session = Depends(get_db)):
    import json
    m = _mgr(request)
    conn = db.get(Connection, cid)
    if not conn:
        raise HTTPException(404, "Connection not found")
    t = m.type(conn.type)
    if t is None:
        raise HTTPException(404, f"Connector type {conn.type} is not installed")
    cfg = json.loads(conn.config_json or "{}")
    return page(request, db, "connect_form.html", t=t, conn=conn, values={**cfg, "name": conn.name}, config=cfg,
                has_secret={k: True for k, v in m.secrets_of(conn).items() if v})


@router.post("/connect/{cid:int}", dependencies=[Depends(verify_csrf)])
async def connect_update(cid: int, request: Request, db: Session = Depends(get_db)):
    m = _mgr(request)
    conn = db.get(Connection, cid)
    if not conn:
        raise HTTPException(404, "Connection not found")
    t = m.type(conn.type)
    form = _form_dict(await request.form())
    cfg, sec = m.split_form(t, form, conn)
    err = _validate(m, t, cfg, sec)
    if err:
        flash(request, "error", err)
        return back(f"/connect/{cid}/edit")
    m.save(db, conn=conn, kind=conn.kind, type_key=conn.type, name=form.get("name") or conn.name, cfg=cfg, sec=sec)
    ok, msg = m.test(db, conn)
    db.commit()
    flash(request, "ok" if ok else "warn", f"Updated '{conn.name}'. " + (msg if ok else "Test failed: " + msg))
    return back("/connect")


@router.post("/connect/{cid:int}/test", dependencies=[Depends(verify_csrf)])
def connect_test(cid: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(Connection, cid)
    if not conn:
        raise HTTPException(404, "Connection not found")
    ok, msg = _mgr(request).test(db, conn)
    db.commit()
    flash(request, "ok" if ok else "error", f"{conn.name}: {msg}")
    return back("/connect")


@router.post("/connect/{cid:int}/toggle", dependencies=[Depends(verify_csrf)])
def connect_toggle(cid: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(Connection, cid)
    if not conn:
        raise HTTPException(404, "Connection not found")
    conn.enabled = not conn.enabled
    if not conn.enabled:
        conn.is_default = False
    flash(request, "ok", f"'{conn.name}' is now {'on' if conn.enabled else 'off'}. " + ("" if conn.enabled else "Orders for its products will wait for you."))
    return back("/connect")


@router.post("/connect/{cid:int}/default", dependencies=[Depends(verify_csrf)])
def connect_default(cid: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(Connection, cid)
    if not conn or conn.kind != "supplier":
        raise HTTPException(404, "Supplier connection not found")
    on = not conn.is_default
    if on:
        db.query(Connection).filter(Connection.kind == "supplier").update({"is_default": False})
    conn.is_default = on
    flash(request, "ok", f"'{conn.name}' " + ("is now the default supplier for products without their own supplier." if on else "is no longer the default."))
    return back("/connect")


@router.post("/connect/{cid:int}/delete", dependencies=[Depends(verify_csrf)])
def connect_delete(cid: int, request: Request, db: Session = Depends(get_db)):
    conn = db.get(Connection, cid)
    if not conn:
        raise HTTPException(404, "Connection not found")
    used = (db.query(Product).filter(Product.supplier_connection_id == cid).count() + db.query(ProductListing).filter(ProductListing.connection_id == cid).count()
            + db.query(Order).filter(Order.store_connection_id == cid).count())
    if used:
        flash(request, "error", f"'{conn.name}' is used by {used} product(s)/listing(s)/order(s). Switch it off instead of deleting it.")
        return back("/connect")
    db.delete(conn)
    flash(request, "ok", f"Removed '{conn.name}'.")
    return back("/connect")


@router.post("/connect/reload", dependencies=[Depends(verify_csrf)])
def connect_reload(request: Request, db: Session = Depends(get_db)):
    m = _mgr(request)
    m.reload()
    flash(request, "ok" if not m.file_errors else "warn", f"Connector files reloaded: {sum(t.source == 'file' for t in m.types())} custom type(s) found"
          + (f", {len(m.file_errors)} file(s) with errors." if m.file_errors else "."))
    return back("/connect")


@router.post("/connect/upload", dependencies=[Depends(verify_csrf)])
async def connect_upload(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not request.app.state.cfg.allow_connector_upload:
        raise HTTPException(403, "Uploading connector files is switched off (ALLOW_CONNECTOR_UPLOAD). Copy the file into the connectors folder instead.")
    name = Path(file.filename or "").name
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}\.py", name) or name.startswith("_"):
        flash(request, "error", "File name must be letters/digits/_/- and end in .py")
        return back("/connect")
    raw = await file.read()
    if len(raw) > 200_000:
        flash(request, "error", "That file is too large for a connector.")
        return back("/connect")
    try:
        ast.parse(raw.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError) as e:
        flash(request, "error", f"That file is not valid Python: {e}")
        return back("/connect")
    m = _mgr(request)
    (m.folder / name).write_bytes(raw)
    m.reload()
    err = m.file_errors.get(name)
    flash(request, "warn" if err else "ok", f"Saved {name}. " + (f"But it failed to load: {err}" if err else "It now appears under 'Add a connection'."))
    return back("/connect")


@router.get("/connect/template/{kind}.py", response_class=PlainTextResponse)
def connect_template(kind: str, request: Request):
    name = "example_supplier.py" if kind == "supplier" else "example_store.py"
    f = request.app.state.cfg.connectors_path / "examples" / name
    if not f.exists():
        f = EXAMPLES / name
    if kind not in {"supplier", "store"} or not f.exists():
        raise HTTPException(404, "Template not found")
    return PlainTextResponse(f.read_text(encoding="utf-8"), headers={"Content-Disposition": f'attachment; filename="my_{kind}_connector.py"'})


# ---- CJ Dropshipping: paste one API key ----------------------------------------------------
@router.post("/connect/cj/easy", dependencies=[Depends(verify_csrf)])
async def cj_easy(request: Request, db: Session = Depends(get_db)):
    """Create (or update) the CJ connection from one API key, make it the default supplier, and test it right away."""
    from ..services import bootstrap
    key = str((await request.form()).get("api_key", "")).strip()
    ok, msg = bootstrap.set_cj_key(db, _mgr(request), key)
    if not key:
        flash(request, "error", msg)
    else:
        flash(request, "ok" if ok else "error", ("CJ jud gaya aur default supplier ban gaya. " if ok else "CJ save hua par test fail: ") + msg)
    return back("/connect")
