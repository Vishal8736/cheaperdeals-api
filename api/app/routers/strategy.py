"""Strategy page: where to sell each product, market prices, markets, exchange rates, autopilot."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..models import Connection, FxRate, Market, MarketPrice, Product, StrategyRec
from ..security import require_login, verify_csrf
from ..services import fx
from ..services import pricefeed as PF
from ..services import settings_service as S
from ..services import strategy as ST
from .deps import get_db, get_rt
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/strategy", response_class=HTMLResponse)
def strategy_page(request: Request, db: Session = Depends(get_db)):
    ST.seed_markets(db)
    db.commit()
    markets = {m.id: m for m in db.query(Market).order_by(Market.id)}
    recs = []
    for r, p in db.query(StrategyRec, Product).join(Product, Product.id == StrategyRec.product_id).all():
        recs.append({"rec": r, "p": p, "m": markets.get(r.market_id)})
    recs.sort(key=lambda x: (ST.RANK.get(x["rec"].status, 9), -x["rec"].profit_usd))
    count = lambda *st: sum(1 for x in recs if x["rec"].status in st)  # noqa: E731
    stats = {"analysed": len(recs), "viable": count("viable"), "applied": count("applied"), "needs": count("needs_check", "needs_rate", "stale"),
             "potential": sum((x["rec"].profit_usd for x in recs if x["rec"].status in ("viable", "applied")), Decimal("0"))}
    stores = [(c.id, c.name) for c in db.query(Connection).filter(Connection.kind == "store", Connection.enabled.is_(True)).order_by(Connection.id)]
    return page(request, db, "strategy.html", recs=recs, stats=stats, pr=ST.params(db), modes=ST.MODES, markets=list(markets.values()), stores=stores,
                rates=db.query(FxRate).order_by(FxRate.code).all(), prices=db.query(MarketPrice).order_by(MarketPrice.observed_at.desc()).limit(60).all(),
                pf_on=S.get_bool(db, "pricefeed_enabled"), pf_keys=all(PF.credentials(db)), pf_last=S.get(db, "last_run_price_watch", ""),
                products=db.query(Product).order_by(Product.title).limit(500).all(), pname={p.id: p.title for p in db.query(Product.id, Product.title)},
                mname=markets)


@router.get("/strategy/product/{pid}", response_class=HTMLResponse)
def strategy_product(pid: int, request: Request, db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "Product not found")
    ST.seed_markets(db)
    db.commit()
    pr = ST.params(db)
    return page(request, db, "strategy_product.html", p=p, evs=ST.plan(db, p, pr), pr=pr, rec=db.query(StrategyRec).filter(StrategyRec.product_id == pid).first())


@router.post("/strategy/settings", dependencies=[Depends(verify_csrf)])
async def strategy_settings(request: Request, db: Session = Depends(get_db)):
    form = {k: str(v) for k, v in (await request.form()).items()}
    errs = ST.save_settings(db, form)
    if errs:
        flash(request, "error", "; ".join(errs))
    else:
        db.commit()
        flash(request, "ok", "Strategy settings saved." + (" Autopilot is ON: prices will change by themselves." if ST.params(db).autopilot else ""))
    return back("/strategy")


@router.post("/strategy/run", dependencies=[Depends(verify_csrf)])
def strategy_run(request: Request, apply_now: Optional[str] = Form(None), db: Session = Depends(get_db), rt=Depends(get_rt)):
    from ..services.agents import track
    with track(db, "strategist", "manual") as run:
        st = ST.run(db, rt, apply_changes=True if apply_now else False)
        if st.get("paused"):
            run.done(0, "Paused")
        else:
            run.done(st["applied"] + st["reverted"], f"Analysed {st['analysed']}, {st['viable']} good, {st['applied']} priced")
    if st.get("paused"):
        flash(request, "warn", "Automation is paused. Resume it first.")
    else:
        flash(request, "ok" if not st["errors"] else "warn", f"Analysed {st['analysed']} product(s): {st['viable']} good opportunities, {st['applied']} priced, "
              f"{st['needs_you']} need you, {st['errors']} errors." + (f" ({st['fx']})" if st["fx"] else ""))
    return back("/strategy")


@router.post("/strategy/apply/{pid}", dependencies=[Depends(verify_csrf)])
def strategy_apply(pid: int, request: Request, market: int = Form(0), db: Session = Depends(get_db), rt=Depends(get_rt)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "Product not found")
    evs = [e for e in ST.plan(db, p) if e.status == "viable" and (not market or e.market.id == market)]
    if not evs:
        flash(request, "error", "There is no safe price to apply for this product right now.")
        return back(f"/strategy/product/{pid}")
    ok, msg = ST.apply(db, rt, p, evs[0], actor="admin", auto_list=ST.params(db).auto_list)
    db.commit()
    flash(request, "ok" if ok else "error", msg)
    return back(f"/strategy/product/{pid}")


@router.post("/strategy/revert/{pid}", dependencies=[Depends(verify_csrf)])
def strategy_revert(pid: int, request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "Product not found")
    ok = ST.revert(db, rt, p, reason="you pressed Revert")
    db.commit()
    flash(request, "ok" if ok else "error", "Back to the normal markup price." if ok else "The store refused the change. See Alerts.")
    return back(f"/strategy/product/{pid}")


@router.post("/strategy/product/{pid}/currency", dependencies=[Depends(verify_csrf)])
def strategy_cost_currency(pid: int, request: Request, cost_currency: str = Form("USD"), db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "Product not found")
    cur = fx.norm(cost_currency)
    if len(cur) != 3 or not cur.isalpha() or fx.get(db, cur) is None:
        flash(request, "error", f"Add an exchange rate for '{cost_currency}' first.")
    else:
        p.cost_currency = "" if cur == "USD" else cur
        db.commit()
        flash(request, "ok", f"Supplier prices for this product are read as {cur} from the next stock sync." if cur != "USD" else "Supplier prices are read as USD.")
    return back(f"/strategy/product/{pid}")


@router.post("/strategy/prices", dependencies=[Depends(verify_csrf)])
def price_add(request: Request, product: str = Form(""), market: int = Form(0), price: str = Form(""), url: str = Form(""), db: Session = Depends(get_db)):
    try:
        p, m = ST.find_product(db, product), db.get(Market, market)
        if m is None:
            raise ValueError("Choose a market")
        ST.record_price(db, p, m, price, url=url, source="manual")
        db.commit()
        flash(request, "ok", f"Saved: '{p.title}' sells for {price} {m.currency} in {m.name}.")
    except ValueError as e:
        flash(request, "error", str(e))
    return back("/strategy")


@router.post("/strategy/prices/import", dependencies=[Depends(verify_csrf)])
async def price_import(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        text = (await file.read())[:2_000_000].decode("utf-8-sig")
        ok, errs = ST.import_prices_csv(db, text)
        db.commit()
        flash(request, "ok" if not errs else "warn", f"{ok} price(s) saved" + (f", {len(errs)} skipped ({'; '.join(errs[:3])})" if errs else "") + ".")
    except UnicodeDecodeError:
        flash(request, "error", "The CSV must be saved as UTF-8.")
    except ValueError as e:
        flash(request, "error", str(e))
    return back("/strategy")


@router.post("/strategy/prices/{mpid}/delete", dependencies=[Depends(verify_csrf)])
def price_delete(mpid: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(MarketPrice, mpid)
    if row:
        db.delete(row)
        db.commit()
    flash(request, "ok", "Price removed.")
    return back("/strategy")


@router.post("/strategy/markets", dependencies=[Depends(verify_csrf)])
def market_save(request: Request, mid: str = Form(""), code: str = Form(""), name: str = Form(""), currency: str = Form("USD"), shipping: str = Form("0"),
                duty: str = Form("0"), tax: str = Form("0"), fee: str = Form("3"), per_kg: str = Form("0"), store_connection: str = Form(""),
                enabled: Optional[str] = Form(None), db: Session = Depends(get_db)):
    try:
        m = ST.save_market(db, mid=int(mid) if mid.isdigit() else None, code=code, name=name, currency=currency, shipping=shipping, duty=duty, tax=tax,
                           fee=fee, per_kg=per_kg, store_connection=store_connection, enabled=enabled is not None)
        db.commit()
        flash(request, "ok", f"Market {m.code} saved.")
    except ValueError as e:
        db.rollback()
        flash(request, "error", str(e))
    return back("/strategy")


@router.post("/strategy/rates", dependencies=[Depends(verify_csrf)])
def rate_save(request: Request, code: str = Form(""), per_usd: str = Form(""), db: Session = Depends(get_db)):
    try:
        fx.set_rate(db, code, per_usd)
        db.commit()
        flash(request, "ok", f"{fx.norm(code)} rate saved and confirmed.")
    except fx.FxError as e:
        flash(request, "error", str(e))
    return back("/strategy")


@router.post("/strategy/rates/refresh", dependencies=[Depends(verify_csrf)])
def rate_refresh(request: Request, db: Session = Depends(get_db)):
    r = fx.refresh(db)
    db.commit()
    flash(request, "ok" if r["ok"] else "error", f"{r['updated']} exchange rate(s) updated." if r["ok"] else r["error"] + ". The saved rates are unchanged.")
    return back("/strategy")


# ---- automatic market prices (eBay) ---------------------------------------------------------
@router.post("/strategy/pricefeed", dependencies=[Depends(verify_csrf)])
async def pricefeed_settings(request: Request, db: Session = Depends(get_db)):
    from ..services import pricefeed as PF
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        if form.get("client_id") or form.get("client_secret"):
            PF.save_credentials(db, form.get("client_id", ""), form.get("client_secret", ""))
        S.set_value(db, "pricefeed_enabled", "true" if form.get("pricefeed_enabled") in ("on", "true", "1") else "false")
        db.commit()
        flash(request, "ok", "Price watch saved." + ("" if all(PF.credentials(db)) else " Add your eBay keys for it to work."))
    except ValueError as e:
        flash(request, "error", str(e))
    return back("/strategy")


@router.post("/strategy/pricefeed/run", dependencies=[Depends(verify_csrf)])
def pricefeed_run(request: Request, db: Session = Depends(get_db), rt=Depends(get_rt)):
    from ..services import pricefeed as PF
    res = PF.run(db, rt)
    db.commit()
    if res.get("off"):
        flash(request, "error", "Price watch is off or has no eBay keys yet.")
    else:
        flash(request, "ok", f"Looked up {res['looked_up']} price(s): {res['updated']} updated, {res['skipped']} skipped (not enough listings)." + (f" {res['errors']} error(s) - see Alerts." if res["errors"] else ""))
    return back("/strategy")


@router.post("/strategy/product/{pid}/search", dependencies=[Depends(verify_csrf)])
def product_search_words(pid: int, request: Request, db: Session = Depends(get_db), query: str = Form("")):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "Product not found")
    p.search_query = query.strip()[:200]
    db.commit()
    flash(request, "ok", "Search words saved." if p.search_query else "Search words cleared: price watch skips this product.")
    return back(f"/strategy/product/{pid}")
