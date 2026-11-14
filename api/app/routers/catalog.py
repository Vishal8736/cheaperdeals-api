"""Catalog browsing + importing, and listing products on stores."""
from __future__ import annotations

from decimal import Decimal

import csv
import io

from fastapi import BackgroundTasks, APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from ..adapters.base import SupplierError
from ..models import Connection, Product
from ..security import require_login, verify_csrf
from ..services import catalog as C
from ..services import settings_service as S
from ..services.agents import track
from .deps import get_db, get_rt
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])


def _sources(db: Session, rt):
    out = [(0, f"Built-in supplier ({rt.supplier.name})")]
    out += [(c.id, c.name) for c in db.query(Connection).filter(Connection.kind == "supplier", Connection.enabled.is_(True)).order_by(Connection.id)]
    return out


def store_targets(db: Session, rt):
    out = [(0, f"Built-in store ({rt.store.name})")]
    out += [(c.id, c.name) for c in db.query(Connection).filter(Connection.kind == "store", Connection.enabled.is_(True)).order_by(Connection.id)]
    return out


@router.get("/catalog", response_class=HTMLResponse)
def catalog_page(request: Request, source: int = 0, q: str = "", pg: int = 1, db: Session = Depends(get_db), rt=Depends(get_rt)):
    items, error, have = [], "", set()
    searched = "go" in request.query_params or bool(q)
    if searched:
        try:
            with track(db, "catalog", "manual") as run:
                items = C.search(db, rt, source, q, max(1, pg), 20)
                have = C.already_imported(db, source, items)
                run.done(len(items), f"Browsed {dict(_sources(db, rt)).get(source, 'supplier')}: {len(items)} product(s) found" + (f" for '{q}'" if q else ""))
        except (SupplierError, NotImplementedError) as e:
            error = str(e)
        except Exception as e:
            error = f"The supplier could not be reached: {e}"
    from ..services import autocatalog as AC
    return page(request, db, "catalog.html", auto=AC.config(db), auto_have=AC.counts(db), sources=_sources(db, rt), source=source, q=q, pg=max(1, pg), items=items, error=error, have=have,
                searched=searched, markup=S.get_decimal(db, "markup_pct"))


@router.post("/catalog/import", dependencies=[Depends(verify_csrf)])
async def catalog_import(request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    form = await request.form()
    source, q, pg = int(form.get("source") or 0), str(form.get("q") or ""), int(form.get("pg") or 1)
    wanted = {str(v) for v in form.getlist("pick")}
    if not wanted:
        flash(request, "warn", "Tick at least one product first.")
        return back(f"/catalog?source={source}&q={q}&pg={pg}&go=1")
    try:
        markup = Decimal(str(form.get("markup") or "")) if str(form.get("markup") or "").strip() else None
        with track(db, "catalog", "manual") as run:
            res = C.import_items(db, rt, source, wanted, q, pg, markup_pct=markup)
            run.done(res["imported"], f"Imported {res['imported']} product(s) as drafts")
    except Exception as e:
        flash(request, "error", f"Import failed: {e}")
        return back(f"/catalog?source={source}&q={q}&pg={pg}&go=1")
    msg = f"Imported {res['imported']} product(s) as drafts." + (f" {res['skipped']} were already imported." if res["skipped"] else "")
    if res["failed"]:
        msg += " Problems: " + "; ".join(res["failed"][:3])
    flash(request, "ok" if res["imported"] else "warn", msg)
    return back("/products?view=draft" if res["imported"] else f"/catalog?source={source}&q={q}&pg={pg}&go=1")


@router.post("/products/list-on-store", dependencies=[Depends(verify_csrf)])
async def list_on_store(request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("pid") if str(x).isdigit()]
    stores = [int(x) for x in form.getlist("store") if str(x).isdigit()]
    if form.get("all_drafts"):
        ids = [p.id for p in db.query(Product).filter(Product.listing_status == "draft").all()]
    if not ids or not stores:
        flash(request, "warn", "Pick at least one product and one place to list it on.")
        return back("/products")
    ok_n = fail = 0
    errs: list[str] = []
    with track(db, "lister", "manual") as run:
        for p in db.query(Product).filter(Product.id.in_(ids)).all():
            for sid in stores:
                ok, msg = C.list_on_store(db, rt, p, sid)
                ok_n += int(ok)
                fail += int(not ok)
                if not ok:
                    errs.append(f"{p.title}: {msg}")
        run.done(ok_n, f"Listed {ok_n} product(s)" + (f", {fail} failed" if fail else ""))
    flash(request, "ok" if not fail else "warn", f"Listed {ok_n} product listing(s)." + (f" {fail} failed: " + "; ".join(errs[:2]) if fail else ""))
    return back("/products")


CSV_COLUMNS = ["title", "sku", "supplier", "supplier_vid", "cost", "price", "stock", "status", "store_product_id", "image_url", "category", "description"]


@router.get("/products/export.csv")
def products_export(db: Session = Depends(get_db)):
    names = {c.id: c.name for c in db.query(Connection).all()}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for p in db.query(Product).order_by(Product.id).all():
        w.writerow([p.title, p.sku, names.get(p.supplier_connection_id, "Built-in supplier") if p.supplier_connection_id else "Built-in supplier", p.supplier_vid,
                    p.supplier_price, p.selling_price, p.stock, p.listing_status, "" if p.store_product_id.startswith("draft-") else p.store_product_id,
                    p.image_url, p.category, p.description])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="cheapedeal-products.csv"'})


def _num(v: str, default: str = "0") -> Decimal:
    try:
        return Decimal((v or default).strip().replace(",", ""))
    except Exception:
        raise ValueError(f"'{v}' is not a number")


@router.post("/products/import-csv", dependencies=[Depends(verify_csrf)])
async def products_import(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """CSV with a header row. Needs 'title'. A row whose sku already exists updates that product; other rows become drafts
    (or listed products if store_product_id is given)."""
    raw = (await file.read())[:2_000_000]
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        flash(request, "error", "The CSV must be saved as UTF-8.")
        return back("/products")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "title" not in (rows[0] or {}):
        flash(request, "error", "The first row must be a header with at least a 'title' column. Use Export CSV to get a template.")
        return back("/products")
    made = upd = 0
    errs: list[str] = []
    import secrets as _sec
    for n, r in enumerate(rows, start=2):
        try:
            title = (r.get("title") or "").strip()
            if not title:
                raise ValueError("empty title")
            sku = (r.get("sku") or "").strip()
            p = db.query(Product).filter(Product.sku == sku).first() if sku else None
            vals = {"title": title[:255], "supplier_vid": (r.get("supplier_vid") or "").strip(), "image_url": (r.get("image_url") or "").strip()[:600],
                    "category": (r.get("category") or "").strip()[:128], "description": r.get("description") or ""}
            if (r.get("cost") or "").strip():
                vals["supplier_price"] = _num(r["cost"])
            if (r.get("price") or "").strip():
                vals["selling_price"] = _num(r["price"])
            if (r.get("stock") or "").strip():
                vals["stock"] = int(_num(r["stock"]))
            if p:
                for k, v in vals.items():
                    if v != "" or k == "title":
                        setattr(p, k, v)
                upd += 1
            else:
                spid = (r.get("store_product_id") or "").strip()
                if spid and db.query(Product).filter(Product.store_product_id == spid).first():
                    raise ValueError(f"store_product_id {spid} already exists")
                db.add(Product(sku=sku, store_product_id=spid or f"draft-{_sec.token_hex(5)}", source="import",
                               listing_status="listed" if spid else "draft", **vals))
                made += 1
        except Exception as e:
            errs.append(f"row {n}: {e}")
    db.flush()
    flash(request, "ok" if not errs else "warn", f"CSV: {made} added, {upd} updated" + (f", {len(errs)} skipped ({'; '.join(errs[:3])})" if errs else "") + ".")
    return back("/products")


@router.post("/catalog/auto/save", dependencies=[Depends(verify_csrf)])
async def auto_save(request: Request, db: Session = Depends(get_db)):
    f = await request.form()
    S.set_value(db, "auto_catalog", "true" if f.get("on") else "false")
    kw = ", ".join(k.strip() for k in str(f.get("keywords") or "").replace("\n", ",").split(",") if k.strip())[:500]
    S.set_value(db, "auto_catalog_keywords", kw)
    for key, lo, hi in (("target", 1, 500), ("batch", 1, 25)):
        try:
            S.set_value(db, f"auto_catalog_{key}", str(max(lo, min(hi, int(float(str(f.get(key) or "0")))))))
        except ValueError:
            pass
    S.set_value(db, "auto_catalog_cursor", "0:1")
    db.commit()
    flash(request, "ok", "Auto catalog ki setting save ho gayi.")
    return back("/catalog")


@router.post("/catalog/auto/run", dependencies=[Depends(verify_csrf)])
def auto_run(request: Request, background: BackgroundTasks):
    from ..services.job_runner import run_job
    database, rt = request.app.state.database, request.app.state.rt

    def go():
        with database.SessionLocal() as s:
            try:
                run_job(s, rt, "catalog_run", trigger="manual")
            except Exception:
                import logging
                logging.getLogger("cheapedeal.jobs").exception("manual catalog run failed")
    background.add_task(go)
    flash(request, "ok", "Catalog agent chal pada hai. 1-2 minute mein Live Agents page par naye products dikhenge.")
    return back("/catalog")
