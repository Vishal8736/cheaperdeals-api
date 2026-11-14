"""v20: business switchboard (per-business ON/OFF), POD designer + publisher (Printful/Printify), per-business storefront filter."""
from __future__ import annotations

import base64
import io
import json
from decimal import Decimal

import pytest
from PIL import Image

from app.adapters.base import SupplierError
from app.config import Config
from app.connectors import affiliate_sources as AS
from app.connectors import pod
from app.models import Connection, Design, PodItem, Product, Setting
from app.services import business as BZ
from app.services import mode as MODE
from app.services import pod_designer as D
from app.services import pod_publish as PP
from app.services import settings_service as S
from app.services import shop
from app.services.job_runner import run_job
from tests.test_affiliate import AWIN, Net, _connect_feed, reset  # noqa: F401
from tests.test_agents_catalog import client, web_cfg  # noqa: F401
from tests.test_web import login, post


@pytest.fixture(autouse=True)
def fresh_cache():
    shop._cache.clear()
    yield
    shop._cache.clear()
    AS.HTTP = None


# ---- designer ----------------------------------------------------------------------------------
def test_designs_render_transparent_png_in_every_style():
    for st in D.STYLES:
        im = Image.open(io.BytesIO(D.render("Good Vibes Only", st, "lagoon")))
        assert im.size == (D.W, D.H) and im.mode == "RGBA"
        assert im.getpixel((5, 5))[3] == 0  # the corner is empty: it prints on any colour
        assert im.getbbox() is not None  # and something is drawn


def test_unsafe_text_is_refused():
    for bad in ("Nike Dad", "Mickey Mouse Club", "Taylor Swift Fan", "BTS Army", "ab", "x" * 60, "हिंदी", "Buy <script>"):
        assert not D.safe_text(bad), bad
    assert D.safe_text("Dog Mom Energy")
    assert all(D.safe_text(t) for v in D.NICHES.values() for t in v)  # the built-in bank passes its own filter


def test_slogans_are_new_each_time(client):
    login(client)
    with client.app.state.database.SessionLocal() as db:
        first = D.slogans(db, "coffee", 2)
        assert len(first) == 2 and all(src == "builtin" for _, src in first)
        d = D.make_design(db, "coffee", first[0][0])
        assert d.png[:4] == b"\x89PNG" and d.token and len(d.token) == 24
        again = [t for t, _ in D.slogans(db, "coffee", 6)]
        assert first[0][0] not in again
        with pytest.raises(ValueError):
            D.make_design(db, "x", "Disney Dad")


# ---- catalog picking -----------------------------------------------------------------------------
def test_pick_title_variants_and_price():
    spec = PP.PRODUCT_TYPES["tshirt"]
    cat = [{"title": "Kids T-Shirt"}, {"title": "Women's Crop T-Shirt"}, {"title": "Bella + Canvas 3001 Unisex T-Shirt"}, {"title": "Gildan T-Shirt"}, {"title": "Mug"}]
    assert PP.pick_title(cat, spec, key=lambda c: c["title"])["title"].startswith("Bella")
    assert PP.pick_title([{"title": "Mug"}], spec, key=lambda c: c["title"]) is None
    vs = [{"id": i, "color": c, "size": s} for i, (c, s) in enumerate([("Navy", "S"), ("Navy", "M"), ("Black", "S"), ("Black", "M"), ("Black", "L")])]
    assert [v["id"] for v in PP.pick_variants(vs, spec)] == [2, 3, 4]  # one preferred colour, all its sizes
    assert len(PP.pick_variants([{"id": 1}, {"id": 2}, {"id": 3}], PP.PRODUCT_TYPES["mug"])) == 2  # no colours: a couple of sizes
    assert PP.retail_price(Decimal("12.95"), Decimal("2.2")) == Decimal("28.99")


# ---- Printify / Printful publish -------------------------------------------------------------------
def _printify_net(posted):
    def products(m, u, h, b):
        if m == "POST":
            posted["product"] = json.loads(b)
            return {"id": "p9"}
        return {"total": 1, "data": [{"id": "p9", "title": posted["product"]["title"], "images": [{"src": "https://i/m.png", "is_default": True}],
                                       "variants": [{"id": 31, "title": "Black / M", "cost": 900, "is_enabled": True, "is_available": True}]}]}

    def upload(m, u, h, b):
        posted["upload"] = json.loads(b)
        return {"id": "img1"}
    return Net({
        "/catalog/blueprints/6/print_providers/29/variants.json": {"variants": [
            {"id": 31, "title": "Black / M", "options": {"color": "Black", "size": "M"}, "placeholders": [{"position": "front"}]},
            {"id": 32, "title": "Black / L", "options": {"color": "Black", "size": "L"}, "placeholders": [{"position": "front"}]},
            {"id": 33, "title": "Navy / M", "options": {"color": "Navy", "size": "M"}, "placeholders": [{"position": "front"}]}]},
        "/catalog/blueprints/6/print_providers.json": [{"id": 29, "title": "Prov"}],
        "/catalog/blueprints.json": [{"id": 5, "title": "Kids Tee"}, {"id": 6, "title": "Unisex Heavy Cotton T-Shirt"}],
        "/uploads/images.json": upload, "/shops.json": [{"id": 77, "title": "S"}],
        "/shops/77/products/p9.json": {"id": "p9", "variants": [{"id": 31, "cost": 900, "is_enabled": True, "is_available": True}]},
        "/shops/77/products.json": products})


def test_printify_publish_design_request_shape():
    posted = {}
    AS.HTTP = _printify_net(posted)
    pr = pod.PrintifySupplier({}, {"api_token": "tok"})
    ref = pr.publish_design(spec=PP.PRODUCT_TYPES["tshirt"], title="Good Vibes Only - T-shirt", description="d", png=b"\x89PNGdata", fixed_price=Decimal("24.99"))
    assert ref == "p9"
    assert base64.b64decode(posted["upload"]["contents"]) == b"\x89PNGdata"
    b = posted["product"]
    assert b["blueprint_id"] == 6 and b["print_provider_id"] == 29
    assert [v["id"] for v in b["variants"]] == [31, 32] and all(v["price"] == 2499 for v in b["variants"])  # one colour, prices in cents
    assert b["print_areas"][0]["placeholders"][0] == {"position": "front", "images": [{"id": "img1", "x": 0.5, "y": 0.5, "scale": 1, "angle": 0}]}
    AS.HTTP = Net({"/catalog/blueprints.json": [{"id": 1, "title": "Coffee Mug"}]})
    with pytest.raises(SupplierError, match="nahi mila"):
        pod.PrintifySupplier({}, {"api_token": "tok"}).publish_design(spec=PP.PRODUCT_TYPES["hoodie"], title="t", description="d", png=b"x", fixed_price=Decimal("1"))


def test_printful_publish_design_request_shape():
    sent = {}

    def create(m, u, h, b):
        sent.update(json.loads(b))
        return {"result": {"id": 555}}
    AS.HTTP = Net({"/products/71": {"result": {"variants": [
        {"id": 4011, "color": "Black", "size": "M", "price": "13.00", "in_stock": True}, {"id": 4012, "color": "Black", "size": "L", "price": "13.00", "in_stock": False},
        {"id": 4013, "color": "White", "size": "M", "price": "13.00", "in_stock": True}]}},
        "/store/products": create,
        "/products": {"result": [{"id": 1, "title": "Kids Tee", "type_name": "T-Shirt"}, {"id": 71, "title": "Unisex Staple T-Shirt", "type_name": "T-Shirt"}]}})
    pf = pod.PrintfulSupplier({}, {"api_token": "tok"})
    assert pf.publish_design(spec=PP.PRODUCT_TYPES["tshirt"], title="Good Vibes", description="d", markup=Decimal("2.2"), design_url="https://x.onrender.com/shop/design/abc.png") == "555"
    assert [v["variant_id"] for v in sent["sync_variants"]] == [4011]  # out of stock skipped; preferred colour only
    assert sent["sync_variants"][0]["files"] == [{"url": "https://x.onrender.com/shop/design/abc.png"}] and sent["sync_variants"][0]["retail_price"] == "28.99"


def test_printful_needs_a_public_https_design_link():
    class C:
        public_url = "http://localhost:8000"
    with pytest.raises(SupplierError, match="public link"):
        PP.design_url(C(), Design(token="t" * 24, png=b"x"))
    C.public_url = "https://shop.onrender.com/"
    assert PP.design_url(C(), Design(token="a" * 24, png=b"x")) == "https://shop.onrender.com/shop/design/" + "a" * 24 + ".png"


# ---- end to end: design -> Printify -> our store ------------------------------------------------------
def _connect_printify(client, tok):
    r = post(client, "/connect/new", tok, type="pod_printify", name="Printify", api_token="tok", shop_id="77")
    assert r.status_code == 303
    client.get("/programs")
    with client.app.state.database.SessionLocal() as db:
        return db.query(Connection).filter(Connection.type == "pod_printify").one().id


def test_pod_agent_makes_designs_pushes_and_lists_them(client):
    tok = login(client)
    posted = {}
    AS.HTTP = _printify_net(posted)
    cid = _connect_printify(client, tok)
    rt, database = client.app.state.rt, client.app.state.database
    with database.SessionLocal() as db:
        S.set_value(db, "pod_types", "tshirt")
        S.set_value(db, "pod_niches", "coffee")
        S.set_value(db, "pod_batch", "2")
        S.set_value(db, "pod_daily", "3")
        db.commit()
        res = run_job(db, rt, "pod_run", trigger="manual")
        db.commit()
        assert res["designs"] == 2 and res["products"] == 2 and res["errors"] == 0, res
        items = db.query(PodItem).all()
        assert {i.status for i in items} == {"listed"}, [(i.status, i.error) for i in items]
        p = db.query(Product).filter(Product.listing_status == "listed").first()
        assert p.supplier_connection_id == cid and p.supplier_price == Decimal("9.00") and p.selling_price > Decimal("9")  # real cost read back, margin guard passed
        assert BZ.product_kinds(db, "pod") is not None
        # the daily cap: 3 per day, 2 already made -> only one more
        res = run_job(db, rt, "pod_run", trigger="manual")
        db.commit()
        assert res["designs"] == 1
        assert run_job(db, rt, "pod_run", trigger="manual")["designs"] == 0
        assert db.query(Design).count() == 3


def test_pod_error_is_stored_not_raised_and_never_lists(client):
    tok = login(client)
    AS.HTTP = Net({"/shops.json": [{"id": 77}], "/shops/77/products.json": {"total": 0, "data": []}, "/catalog/blueprints.json": [{"id": 1, "title": "Coffee Mug"}]})
    cid = _connect_printify(client, tok)
    rt, database = client.app.state.rt, client.app.state.database
    with database.SessionLocal() as db:
        d = D.make_design(db, "coffee", "Brew Crew")
        it = PP.publish_item(db, rt, rt.cfg, d, db.get(Connection, cid), "hoodie")
        assert it.status == "error" and "nahi mila" in it.error
        assert db.query(Product).count() == 0


# ---- switchboard -------------------------------------------------------------------------------------------
def test_flag_defaults_and_job_gating(client):
    login(client)
    rt, database = client.app.state.rt, client.app.state.database
    with database.SessionLocal() as db:
        db.query(Setting).filter(Setting.key.like("biz\\_%", escape="\\")).delete(synchronize_session=False)
        db.commit()
        assert MODE.is_on(db, "affiliate") and not MODE.is_on(db, "pod") and not MODE.is_on(db, "dropship") and not MODE.is_on(db, "ebook")
        assert MODE.is_affiliate(db)
        assert run_job(db, rt, "pod_run").get("skipped") == "business off"
        assert run_job(db, rt, "catalog_run").get("skipped") == "affiliate mode"
        MODE.set_on(db, "pod", True)
        assert not MODE.is_affiliate(db) and MODE.job_allowed(db, "process_pending") and MODE.job_allowed(db, "pod_run")
        assert not MODE.job_allowed(db, "catalog_run")  # catalog (CJ) belongs to dropship only
        MODE.set_on(db, "affiliate", False)
        assert not MODE.job_allowed(db, "affiliate_run") and not MODE.job_allowed(db, "earnings_run")
        assert MODE.set_on(db, "ebook", True) is False and not MODE.is_on(db, "ebook")  # not built yet: can never be ON


def test_biz_on_env_seeds_the_flags(monkeypatch):
    monkeypatch.setenv("BIZ_ON", "affiliate, pod")
    assert Config().biz_on == "affiliate, pod"


def test_switchboard_page_toggle_bulk_and_nav(client):
    tok = login(client)
    html = client.get("/business").text
    assert "Business switchboard" in html and "jaldi (v21)" in html and "Ebook" in html
    with client.app.state.database.SessionLocal() as db:
        MODE.set_on(db, "pod", False)
        MODE.set_on(db, "dropship", False)
        db.commit()
    assert 'href="/pod"' not in client.get("/").text  # POD menu only when POD is on
    r = client.post("/business/toggle?key=pod", data={"on": "1"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 303
    with client.app.state.database.SessionLocal() as db:
        assert MODE.is_on(db, "pod") and MODE.is_on(db, "affiliate")  # one business switched, the other untouched
    client.get("/business")
    assert 'href="/pod"' in client.get("/").text
    client.post("/business/toggle?key=ebook", data={"on": "1"}, headers={"X-CSRF-Token": tok})
    client.get("/business")
    r = client.post("/business/bulk", data={"action": "stop", "keys": ["pod", "affiliate"]}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 303
    with client.app.state.database.SessionLocal() as db:
        assert not MODE.is_on(db, "pod") and not MODE.is_on(db, "affiliate") and not MODE.is_on(db, "ebook")
    client.get("/business")
    r = client.post("/business/bulk", data={"action": "start", "keys": ["pod", "affiliate", "ebook"]}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 303
    with client.app.state.database.SessionLocal() as db:
        assert MODE.is_on(db, "pod") and MODE.is_on(db, "affiliate") and not MODE.is_on(db, "ebook")
    client.get("/business")
    r = client.post("/business/bulk", data={"action": "start"}, headers={"X-CSRF-Token": tok})  # nothing ticked
    assert r.status_code == 303 and "kam se kam ek" in client.get("/business").text


def test_pod_page_make_and_public_design_link(client):
    tok = login(client)
    with client.app.state.database.SessionLocal() as db:
        MODE.set_on(db, "pod", True)
        db.commit()
    r = post(client, "/pod/make", tok, niche="coffee", text="Brew Crew", push="0")
    assert r.status_code == 303
    html = client.get("/pod").text
    assert "Brew Crew" in html and "Print-on-demand" in html
    with client.app.state.database.SessionLocal() as db:
        d = db.query(Design).one()
        token, did = d.token, d.id
    png = client.get(f"/shop/design/{token}.png")
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG" and "noindex" in png.headers["x-robots-tag"]
    assert client.get("/shop/design/" + "0" * 24 + ".png").status_code == 404
    assert client.get("/shop/design/../x.png").status_code in (404, 422)
    assert client.get(f"/pod/design/{did}.png").status_code == 200
    r = post(client, "/pod/make", tok, niche="coffee", text="Nike Run Club", push="0")
    assert "surakshit nahi" in client.get("/pod").text
    r = post(client, "/pod/settings", tok, niches="coffee, dogs", types=["mug", "tshirt", "bogus"], pod_daily="99", pod_batch="2", pod_markup="2.5", price_mug="18.50")
    with client.app.state.database.SessionLocal() as db:
        c = D.config(db)
        assert c["types"] == ["mug", "tshirt"] and c["daily"] == 30 and c["niches"] == ["coffee", "dogs"] and S.get(db, "pod_price_mug") == "18.50"
    client.get("/pod")
    # supplier push with no POD supplier connected says so
    post(client, f"/pod/publish/{did}", tok)
    assert "Printful ya Printify jodo" in client.get("/pod").text


# ---- storefront per business ------------------------------------------------------------------------------------
def test_storefront_filters_by_business(client):
    tok = login(client)
    cid = _connect_feed(client, tok, {"feed.example": AWIN})
    post(client, f"/programs/{cid}/import", tok, keywords="", limit="5")
    posted = {}
    AS.HTTP = _printify_net(posted)
    pid_conn = _connect_printify(client, tok)
    rt, database = client.app.state.rt, client.app.state.database
    with database.SessionLocal() as db:
        for k in ("affiliate", "pod", "dropship"):
            MODE.set_on(db, k, True)
        S.set_value(db, "pod_types", "tshirt")
        d = D.make_design(db, "coffee", "Brew Crew")
        PP.publish_item(db, rt, rt.cfg, d, db.get(Connection, pid_conn), "tshirt")
        db.add(Product(store_product_id="D-1", title="Plain dropship thing", selling_price=Decimal("9"), supplier_price=Decimal("3"), stock=5, listing_status="listed"))
        db.commit()
    shop._cache.clear()
    names = lambda biz: sorted(p["title"] for p in client.get("/shop/products" + (f"?biz={biz}" if biz else "")).json()["products"])  # noqa: E731
    assert len(names("")) == 4  # two affiliate, one pod, one dropship
    assert names("affiliate") == ["Blue Mug", "Red Mug"]
    assert len(names("pod")) == 1 and "Brew Crew" in names("pod")[0]
    assert names("dropship") == ["Plain dropship thing"]
    cfg = lambda biz: client.get("/shop/config" + (f"?biz={biz}" if biz else "")).json()["mode"]  # noqa: E731
    assert cfg("affiliate") == "affiliate" and cfg("pod") == "dropship" and cfg("") == "dropship"  # the affiliate site has no cart, the pod site does
    with database.SessionLocal() as db:
        MODE.set_on(db, "pod", False)
        db.commit()
    shop._cache.clear()
    assert names("pod") == [] and len(names("")) == 3  # switching a business off removes its products from the website
    assert cfg("pod") == "affiliate"


def test_storefront_js_and_config_know_the_business():
    src = open("api/app/storefront/app.js", encoding="utf-8").read()
    assert "window.SHOP_BIZ" in src and '"/shop/products" + Q' in src and '"/shop/config" + Q' in src
    assert 'window.SHOP_BIZ = ""' in open("api/app/storefront/config.js", encoding="utf-8").read()
