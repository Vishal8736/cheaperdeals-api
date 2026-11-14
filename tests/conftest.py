from __future__ import annotations

from decimal import Decimal

import os

import pytest

from app.adapters.base import NormalizedItem, NormalizedOrder
from app.adapters.mock_store import MockStore
from app.adapters.mock_supplier import MockSupplier
from app.adapters.telegram import MemoryNotifier
from app.config import Config
from app.db import Database
from app.models import Product
from app.services import settings_service as S
from app.services.orders import Runtime


def db_url_for(tmp_path, name="t"):
    """SQLite by default; set TEST_DATABASE_URL=postgresql://... to run the whole suite against real Postgres."""
    return os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/{name}.db"


@pytest.fixture
def cfg(tmp_path):
    return Config(app_env="test", database_url=db_url_for(tmp_path), secret_key="x" * 32,
                  internal_api_key="k" * 32, admin_password="pw-for-tests", timezone="UTC")


@pytest.fixture
def database(cfg):
    d = Database(cfg.database_url)
    d.drop_all()
    d.create_all()
    yield d
    d.engine.dispose()


@pytest.fixture
def db(database, cfg):
    s = database.SessionLocal()
    S.ensure_defaults(s, cfg)
    yield s
    s.close()


@pytest.fixture
def supplier():
    sup = MockSupplier()
    sup.set_product("VID-1", stock=50, price="10.00")
    return sup


@pytest.fixture
def store():
    return MockStore()


@pytest.fixture
def notifier():
    return MemoryNotifier()


@pytest.fixture
def rt(cfg, supplier, store, notifier):
    return Runtime(cfg=cfg, supplier=supplier, store=store, notifier=notifier)


@pytest.fixture
def live(db):
    """Switch to live mode (not dry-run) with a shipping method configured."""
    S.set_value(db, "dry_run", "false")
    S.set_value(db, "default_logistic_name", "TestPost")
    db.commit()


@pytest.fixture
def product(db):
    p = Product(store_product_id="P1", sku="SKU1", title="Desk Lamp", supplier_vid="VID-1",
                supplier_price=Decimal("10.00"), selling_price=Decimal("25.00"), stock=50)
    db.add(p)
    db.commit()
    return p


def make_norm(order_id="1001", *, qty=1, unit="25.00", pid="P1", total=None, paid=True, status="processing"):
    unit_d = Decimal(unit)
    return NormalizedOrder(
        store_order_id=str(order_id), number=str(order_id), status=status, paid=paid, customer_name="Asha Rao",
        email="asha@example.com", phone="+911234567890", address1="12 MG Road", address2="", city="Pune", state="MH",
        zip_code="411001", country_code="IN", country="India", total=Decimal(total) if total else unit_d * qty,
        currency="USD", items=[NormalizedItem(pid, "SKU1", "Desk Lamp", qty, unit_d)],
    )


@pytest.fixture(autouse=True)
def _no_cj_pacing(monkeypatch):
    """The real CJ connector keeps 1 second between calls; tests must not wait for it."""
    from app.connectors import cj
    monkeypatch.setattr(cj, "MIN_GAP", 0.0)
