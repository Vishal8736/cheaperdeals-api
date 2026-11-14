from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timezone
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from .adapters.mock_store import MockStore
from .adapters.mock_supplier import MockSupplier
from .adapters.no_supplier import NoSupplier
from .adapters.telegram import NullNotifier, TelegramNotifier
from .adapters.woocommerce import WooCommerceStore
from .version import VERSION
from .config import Config, get_config
from .connectors.manager import ConnectorManager
from .db import Database
from .security import LoginLimiter, NotAuthenticated, csrf_token, hash_password
from .services import settings_service as S
from .services.orders import Runtime

BASE = Path(__file__).parent
log = logging.getLogger("cheapedeal")


def build_runtime(cfg: Config, database: Database) -> Runtime:
    # Only tests (and an explicit ENABLE_DEV_TOOLS=true practice run) get the simulated supplier. A real run without a connected supplier gets NoSupplier,
    # which refuses honestly instead of inventing 'supplier orders'.
    supplier = MockSupplier() if (cfg.app_env == "test" or cfg.enable_dev_tools) else NoSupplier()
    store = (WooCommerceStore(cfg.woo_base_url, cfg.woo_consumer_key, cfg.woo_consumer_secret, cfg.woo_webhook_secret)
             if cfg.store_backend == "woocommerce" else MockStore())
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id) if cfg.telegram_bot_token else NullNotifier()
    return Runtime(cfg=cfg, supplier=supplier, store=store, notifier=notifier)


def create_app(cfg: Config | None = None, runtime: Runtime | None = None) -> FastAPI:
    cfg = cfg or get_config()
    cfg.validate()
    database = Database(cfg.database_url)
    from .services import diag as _diag

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.create_all()
        _diag.install(database, cfg.app_env)
        log.info("App start: env=%s, database=%s", cfg.app_env, "postgres" if cfg.database_url.startswith("postgres") else "sqlite")
        with database.session() as s:
            S.ensure_defaults(s, cfg)
            from .services import strategy as _st
            _st.seed_markets(s)
        with database.session() as s:
            from .services import bootstrap as _bs
            _bs.disable_removed_connections(s, app.state.connectors)
            log.info("CJ: %s", _bs.ensure_cj(s, app.state.connectors, cfg.cj_api_key))
        if cfg.app_env != "test":  # test the connected supplier in the background so it shows 'Connected' (ready) by itself
            import threading
            threading.Thread(target=_bs.verify_suppliers, args=(database, app.state.connectors), name="supplier-selftest", daemon=True).start()
        sched = None
        if cfg.internal_scheduler:
            from .scheduler import Scheduler
            sched = Scheduler(database, app.state.rt)
            sched.start()
        app.state.scheduler = sched
        from .services import keepalive as _ka
        ka = _ka.start(cfg.keepalive_url) if cfg.app_env != "test" else None
        yield
        log.info("App stop")
        _diag.flush()
        if ka is not None:
            ka.set()
        if sched is not None:
            sched.stop()

    app = FastAPI(title="Cheape Deal", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.database = database
    app.state.rt = runtime or build_runtime(cfg, database)
    from .services import mailer as _mailer
    _mailer.SECRET_KEY = cfg.secret_key
    from .services import pricefeed as _pf
    _pf.SECRET_KEY = cfg.secret_key
    from .services import assistant as _ai
    _ai.SECRET_KEY = cfg.secret_key
    app.state.connectors = ConnectorManager(database, cfg.secret_key, cfg.connectors_path)
    app.state.connectors.allow_demo = cfg.app_env == "test" or cfg.enable_dev_tools
    if app.state.rt.connectors is None:
        app.state.rt.connectors = app.state.connectors
    app.state.limiter = LoginLimiter()
    app.state.admin_hash = cfg.admin_password_hash or hash_password(cfg.admin_password)

    from .i18n import HinglishMiddleware
    HinglishMiddleware.default = "en" if cfg.app_env == "test" else "hi"
    app.add_middleware(HinglishMiddleware)  # added first = runs inside the session middleware, so it can read the language choice
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, session_cookie="cd_session",
                       https_only=cfg.is_prod, same_site="lax", max_age=7 * 24 * 3600)

    if cfg.shop_origins.strip():  # only needed if the website calls this server directly instead of through Vercel's /api rewrite
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in cfg.shop_origins.split(",") if o.strip()],
                           allow_methods=["GET", "POST"], allow_headers=["Content-Type", "If-None-Match"], max_age=86400)
    from .services import shop as _shop
    _shop.configure(cfg.secret_key)

    templates = Jinja2Templates(directory=str(BASE / "templates"))
    tz = cfg.tzinfo

    def money(v) -> str:
        try:
            return f"{cfg.currency_symbol}{Decimal(v):,.2f}"
        except Exception:
            return "-"

    def dt(v, fmt: str = "%d %b, %H:%M") -> str:
        if not v:
            return "-"
        return v.replace(tzinfo=timezone.utc).astimezone(tz).strftime(fmt)

    def label(state: str) -> str:
        return {"submitted_to_supplier": "With supplier", "supplier_confirmed": "Supplier confirmed",
                "pending_approval": "Needs approval", "failed_manual": "Needs you", "failed_retry": "Retrying",
                }.get(state, state.replace("_", " ").capitalize())

    def num(v) -> str:
        try:
            t = format(Decimal(v).normalize(), "f")
        except Exception:
            return str(v)
        return t

    templates.env.filters.update(money=money, dt=dt, label=label, num=num)
    templates.env.globals.update(cfg=cfg, csrf_token=csrf_token)
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    import time as _time

    QUIET = ("/static", "/ping", "/health", "/favicon", "/manifest", "/diagnostics", "/internal/engine/status")

    @app.middleware("http")
    async def _trace_requests(request: Request, call_next):
        """Gives every request an ID (X-Request-ID), logs crashes with the full trace, and shows the person an Error ID instead of a blank page."""
        rid = _diag.new_ref()
        request.state.rid = rid
        path = request.url.path
        t0 = _time.time()
        try:
            resp = await call_next(request)
        except Exception:
            logging.getLogger("cheapedeal.web").exception("%s %s crashed", request.method, path, extra={"cat": "web", "ref": rid, "path": path})
            wants_html = "text/html" in request.headers.get("accept", "") and not path.startswith(("/internal", "/webhooks", "/api"))
            msg = f"Kuch gadbad ho gayi. Error ID: {rid}. Admin ho to Diagnostics page par ye ID dhundo (ya ID mujhe bhejo)."
            if wants_html:
                resp = templates.TemplateResponse(request, "error.html", {"code": 500, "message": msg, "user": None}, status_code=500)
            else:
                resp = JSONResponse({"detail": "internal error", "error_id": rid}, status_code=500)
            resp.headers["X-Request-ID"] = rid
            return resp
        secs = _time.time() - t0
        resp.headers["X-Request-ID"] = rid
        if not path.startswith(QUIET):
            lg = logging.getLogger("cheapedeal.web")
            extra = {"cat": "web", "ref": rid, "path": path}
            if resp.status_code >= 500:
                lg.error("%s %s -> %s (%.1fs)", request.method, path, resp.status_code, secs, extra=extra)
            elif resp.status_code in (401, 403) and request.method != "GET":
                lg.warning("%s %s -> %s (blocked: login/CSRF/permission)", request.method, path, resp.status_code, extra={**extra, "cat": "security"})
            elif secs > (12 if request.method == "POST" and path.startswith(("/keys/", "/website/", "/engine/", "/agents/", "/catalog/auto", "/ai/")) else 3):
                lg.warning("slow request %s %s took %.1fs", request.method, path, secs, extra=extra)  # (key tests, hosting and agent runs are slow on purpose)
        return resp

    @app.exception_handler(NotAuthenticated)
    async def _not_auth(request: Request, exc: NotAuthenticated):
        if request.headers.get("hx-request"):
            return HTMLResponse("", status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        wants_html = "text/html" in request.headers.get("accept", "") and not request.url.path.startswith(("/internal", "/webhooks"))
        if wants_html and exc.status_code in {403, 404}:
            return templates.TemplateResponse(request, "error.html", {"code": exc.status_code, "message": exc.detail, "user": None},
                                              status_code=exc.status_code)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.api_route("/ping", methods=["GET", "HEAD"], include_in_schema=False)
    def ping():
        """Keep-alive target: no database, no work. A free pinger (GitHub Actions / UptimeRobot) calls this every ~10 minutes."""
        return Response("ok", media_type="text/plain", headers={"Cache-Control": "no-store"})

    @app.get("/health")
    def health():
        try:
            with database.session() as s:
                s.execute(text("SELECT 1"))
        except Exception as e:
            return JSONResponse({"ok": False, "db": str(e)[:100]}, status_code=503)
        return {"ok": True, "version": VERSION}

    from .routers import diagnostics as diag_ui, host, keys as keys_ui, platforms as platforms_ui, shop_api, support as support_ui, website, agents_ui, analytics, assistant, auth, email as email_ui, catalog, connect, feed, internal, ops, playbook, programs, earnings as earnings_ui, business as business_ui, pod as pod_ui, strategy, ui, users, webhooks
    app.include_router(auth.router)
    app.include_router(webhooks.router)
    app.include_router(internal.router)
    app.include_router(feed.router)
    app.include_router(ops.router)
    app.include_router(ops.auth)
    app.include_router(ops.api)
    app.include_router(programs.router)
    app.include_router(earnings_ui.router)
    app.include_router(business_ui.router)
    app.include_router(pod_ui.router)
    app.include_router(catalog.router)  # before ui: /products/list-on-store must win over /products/{pid}
    app.include_router(users.router)
    app.include_router(ui.router)
    app.include_router(agents_ui.router)
    app.include_router(connect.router)
    app.include_router(analytics.router)
    app.include_router(strategy.router)
    app.include_router(playbook.router)
    app.include_router(assistant.router)
    app.include_router(email_ui.router)
    app.include_router(shop_api.router)
    app.include_router(website.router)
    app.include_router(host.router)
    app.include_router(diag_ui.router)
    app.include_router(keys_ui.router)
    app.include_router(platforms_ui.router)
    app.include_router(support_ui.router)
    return app


def app_factory() -> FastAPI:  # uvicorn --factory app.main:app_factory
    return create_app()


class _LazyApp:
    """`uvicorn app.main:app` works without importing config at import time (tests build apps with create_app)."""

    def __init__(self) -> None:
        self._app: FastAPI | None = None

    async def __call__(self, scope, receive, send):
        if self._app is None:
            self._app = create_app()
        await self._app(scope, receive, send)


app = _LazyApp()
