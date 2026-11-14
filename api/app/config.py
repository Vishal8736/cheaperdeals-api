"""Environment-driven configuration. Runtime-tunable business rules live in the DB (see services/settings_service.py)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    app_env: str = field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./cheapedeal.db"))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    internal_api_key: str = field(default_factory=lambda: os.getenv("INTERNAL_API_KEY", ""))
    admin_username: str = field(default_factory=lambda: os.getenv("ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: os.getenv("ADMIN_PASSWORD", ""))
    admin_password_hash: str = field(default_factory=lambda: os.getenv("ADMIN_PASSWORD_HASH", ""))
    timezone: str = field(default_factory=lambda: os.getenv("APP_TIMEZONE", "Asia/Kolkata"))
    currency_symbol: str = field(default_factory=lambda: os.getenv("CURRENCY_SYMBOL", "$"))
    public_url: str = field(default_factory=lambda: os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8000")

    shop_origins: str = field(default_factory=lambda: os.getenv("SHOP_ORIGINS", ""))  # website addresses allowed to call /shop directly (comma separated)
    keepalive_url: str = field(default_factory=lambda: os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL", ""))  # this server's public address: it pings itself while awake
    cj_api_key: str = field(default_factory=lambda: os.getenv("CJ_API_KEY", ""))
    supplier_backend: str = field(default_factory=lambda: os.getenv("SUPPLIER_BACKEND", "mock"))  # mock only: the real supplier is CJ Dropshipping (Connect page)
    store_backend: str = field(default_factory=lambda: os.getenv("STORE_BACKEND", "mock"))  # mock | woocommerce


    woo_base_url: str = field(default_factory=lambda: os.getenv("WOO_BASE_URL", ""))
    woo_consumer_key: str = field(default_factory=lambda: os.getenv("WOO_CONSUMER_KEY", ""))
    woo_consumer_secret: str = field(default_factory=lambda: os.getenv("WOO_CONSUMER_SECRET", ""))
    woo_webhook_secret: str = field(default_factory=lambda: os.getenv("WOO_WEBHOOK_SECRET", ""))

    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    internal_scheduler: bool = field(default_factory=lambda: _b("INTERNAL_SCHEDULER", False))  # run the agents inside the app (no n8n)
    connectors_dir: str = field(default_factory=lambda: os.getenv("CONNECTORS_DIR", ""))
    allow_connector_upload: bool = field(default_factory=lambda: _b("ALLOW_CONNECTOR_UPLOAD", False))

    enable_dev_tools: bool = field(default_factory=lambda: _b("ENABLE_DEV_TOOLS", False))
    # Default for the DB setting on first boot only. After that, the dashboard toggle is the truth.
    # v19: "affiliate" (default: partner links + network earnings) or "dropship" (the older sell-and-ship flow). First boot only; Settings changes it after that.
    business_mode: str = field(default_factory=lambda: "dropship" if os.getenv("BUSINESS_MODE", "affiliate").strip().lower() == "dropship" else "affiliate")
    biz_on: str = field(default_factory=lambda: os.getenv("BIZ_ON", ""))  # v20: comma list of businesses switched on at first boot (affiliate,pod,dropship)
    dry_run_default: bool = field(default_factory=lambda: _b("DRY_RUN", True))

    @property
    def tzinfo(self):
        """Configured timezone; falls back to UTC (with a log line) if the tz database is missing, e.g. Windows without tzdata."""
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.timezone)
        except Exception:
            import logging
            from datetime import timezone as _tz
            logging.getLogger("cheapedeal").warning("Timezone %r not available; using UTC. pip install tzdata", self.timezone)
            return _tz.utc

    @property
    def connectors_path(self):
        from pathlib import Path
        return Path(self.connectors_dir) if self.connectors_dir else Path(__file__).resolve().parents[2] / "connectors"

    @property
    def is_prod(self) -> bool:
        return self.app_env == "production"

    @property
    def is_postgres(self) -> bool:
        return self.database_url.strip().startswith(("postgresql", "postgres://"))

    def validate(self) -> None:
        problems = []
        if not self.secret_key or len(self.secret_key) < 16:
            problems.append("SECRET_KEY must be set (16+ chars)")
        if not self.internal_api_key or len(self.internal_api_key) < 16:
            problems.append("INTERNAL_API_KEY must be set (16+ chars) - n8n uses it")
        if not (self.admin_password or self.admin_password_hash):
            problems.append("Set ADMIN_PASSWORD (or ADMIN_PASSWORD_HASH)")
        if self.store_backend == "woocommerce" and not (self.woo_base_url and self.woo_consumer_key and self.woo_consumer_secret):
            problems.append("STORE_BACKEND=woocommerce needs WOO_BASE_URL, WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET")
        if self.store_backend == "woocommerce" and not self.woo_webhook_secret:
            problems.append("STORE_BACKEND=woocommerce needs WOO_WEBHOOK_SECRET (webhook signature check)")
        if self.is_prod and self.enable_dev_tools:
            problems.append("ENABLE_DEV_TOOLS must be false in production")
        if problems:
            raise RuntimeError("Config problems:\n - " + "\n - ".join(problems))


def load_env_file(path: str | os.PathLike) -> bool:
    """Tiny .env reader (no dependency) so `uvicorn app.main:app` works on Windows without Docker.
    Real environment variables always win. Surrounding quotes are stripped."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if val[:1] in {"'", '"'} and val[-1:] == val[:1] and len(val) >= 2:
            val = val[1:-1]
        elif " #" in val:  # inline comment
            val = val.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key.strip(), val)
    return True


def get_config() -> Config:
    if os.environ.get("CHEAPEDEAL_NO_DOTENV"):
        return Config()
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(os.getcwd(), ".env"), os.path.join(here, "..", "..", ".env"), os.path.join(here, "..", ".env")):
        if load_env_file(candidate):
            break
    return Config()
