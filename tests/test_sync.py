from __future__ import annotations

from decimal import Decimal

from app.adapters.base import StoreError, SupplierError
from app.models import Alert, DELIVERED, Order, PriceHistory, SHIPPED, SUBMITTED, TrackingEvent
from app.services import orders as O
from app.services import settings_service as S
from app.services import sync as Y
from tests.conftest import make_norm


def place(db, rt, oid="200"):
    order, *_ = O.intake(db, make_norm(oid))
    db.commit()
    assert O.process_order(db, rt, order.id)["outcome"] == "submitted"
    return db.get(Order, order.id)


# ---- tracking ------------------------------------------------------------------------------
def test_tracking_notifies_customer_once_per_status(db, rt, supplier, store, product, live):
    order = place(db, rt)
    sid = order.supplier_order.supplier_order_id
    supplier.advance(sid, "shipped", tracking="TRK123", carrier="DHL")
    Y.tracking_sync(db, rt)
    Y.tracking_sync(db, rt)  # second run: nothing new -> no second email
    Y.tracking_sync(db, rt)
    customer_notes = [n for n in store.notes if n[2] is True]
    assert len(customer_notes) == 1 and "TRK123" in customer_notes[0][1]
    assert db.get(Order, order.id).status == SHIPPED
    assert db.query(TrackingEvent).filter_by(order_id=order.id).count() == 1


def test_delivered_completes_store_order(db, rt, supplier, store, product, live):
    order = place(db, rt, "201")
    sid = order.supplier_order.supplier_order_id
    supplier.advance(sid, "shipped", tracking="T1")
    Y.tracking_sync(db, rt)
    supplier.advance(sid, "delivered")
    Y.tracking_sync(db, rt)
    assert db.get(Order, order.id).status == DELIVERED
    assert store.statuses[order.store_order_id] == "completed"
    assert len([n for n in store.notes if n[2]]) == 2  # shipped + delivered


def test_status_never_moves_backwards(db, rt, supplier, product, live):
    order = place(db, rt, "202")
    sid = order.supplier_order.supplier_order_id
    supplier.advance(sid, "shipped", tracking="T2")
    Y.tracking_sync(db, rt)
    supplier.advance(sid, "confirmed")  # supplier glitch
    Y.tracking_sync(db, rt)
    assert db.get(Order, order.id).status == SHIPPED


def test_supplier_cancel_raises_alert(db, rt, supplier, notifier, product, live):
    order = place(db, rt, "203")
    supplier.advance(order.supplier_order.supplier_order_id, "cancelled")
    Y.tracking_sync(db, rt)
    assert db.get(Order, order.id).status == "cancelled"
    assert any("cancelled order" in m for m in notifier.sent)


def test_store_down_during_notify_is_retried_next_run(db, rt, supplier, store, product, live):
    order = place(db, rt, "204")
    supplier.advance(order.supplier_order.supplier_order_id, "shipped", tracking="T3")
    original = store.add_order_note

    def boom(*a, **k):
        raise StoreError("store down")

    store.add_order_note = boom
    Y.tracking_sync(db, rt)
    assert db.query(TrackingEvent).filter_by(customer_notified=False).count() == 1
    store.add_order_note = original
    Y.tracking_sync(db, rt)
    assert db.query(TrackingEvent).filter_by(customer_notified=False).count() == 0
    assert len([n for n in store.notes if n[2]]) == 1


def test_tracking_skips_dry_run_orders(db, rt, supplier, product):
    order, *_ = O.intake(db, make_norm("205"))
    db.commit()
    O.process_order(db, rt, order.id)  # dry run
    assert Y.tracking_sync(db, rt)["checked"] == 0


# ---- stock + price -------------------------------------------------------------------------
def test_out_of_stock_hides_product_then_restock_unhides(db, rt, supplier, store, product):
    supplier.set_product("VID-1", stock=0, price="10.00")
    Y.stock_price_sync(db, rt)
    assert product.auto_hidden and store.visible["P1"] is False
    supplier.set_product("VID-1", stock=20, price="10.00")
    Y.stock_price_sync(db, rt)
    assert not product.auto_hidden and store.visible["P1"] is True


def test_supplier_price_rise_reprices_with_markup(db, rt, supplier, store, product):
    S.set_value(db, "markup_pct", "100")
    db.commit()
    supplier.set_product("VID-1", stock=10, price="12.00")
    stats = Y.stock_price_sync(db, rt)
    assert stats["repriced"] == 1
    assert product.selling_price == Decimal("24.00") and store.prices["P1"] == Decimal("24.00")
    assert db.query(PriceHistory).count() == 1


def test_manual_override_not_repriced_but_hidden_if_loss(db, rt, supplier, store, product):
    product.manual_override = True
    db.commit()
    supplier.set_product("VID-1", stock=10, price="24.00")  # my fixed 25.00 price now has ~4% margin
    Y.stock_price_sync(db, rt)
    assert product.selling_price == Decimal("25.00") and "P1" not in store.prices
    assert product.auto_hidden and "Margin" in product.hidden_reason and store.visible["P1"] is False


def test_never_sells_at_loss_even_with_negative_markup_path(db, rt, supplier, store, product):
    S.set_value(db, "markup_pct", "0")  # reprice to cost = 0% margin -> must hide
    db.commit()
    supplier.set_product("VID-1", stock=10, price="15.00")
    Y.stock_price_sync(db, rt)
    assert product.auto_hidden is True


def test_operator_disabled_product_not_unhidden_by_sync(db, rt, supplier, store, product):
    supplier.set_product("VID-1", stock=0, price="10.00")
    Y.stock_price_sync(db, rt)
    product.active = False
    db.commit()
    supplier.set_product("VID-1", stock=20, price="10.00")
    Y.stock_price_sync(db, rt)
    assert product.auto_hidden is False and store.visible["P1"] is False  # stays off in store


def test_supplier_read_failure_is_alerted_not_fatal(db, rt, supplier, notifier, product):
    supplier.products.pop("VID-1")
    stats = Y.stock_price_sync(db, rt)
    assert stats["errors"] == 1 and any("Could not read supplier data" in m for m in notifier.sent)


# ---- sweep + reconcile ---------------------------------------------------------------------
def woo_like(oid):
    return {"id": oid, "status": "processing", "name": "A B", "email": "a@b.c", "total": "25.00",
            "items": [{"store_product_id": "P1", "sku": "SKU1", "title": "Desk Lamp", "quantity": 1, "unit_price": "25.00"}]}


def test_sweep_imports_order_whose_webhook_was_lost(db, rt, store, product):
    store.recent_orders = [woo_like(300)]
    res = Y.sweep_missed_orders(db, rt)
    assert res["ingested"] == 1 and db.query(Order).filter_by(store_order_id="300").count() == 1
    assert Y.sweep_missed_orders(db, rt)["missing"] == 0  # idempotent


def test_reconcile_reports_missing_order_without_importing(db, rt, store, notifier, product):
    store.recent_orders = [woo_like(301)]
    rep = Y.reconcile(db, rt)
    assert rep["problems"] and db.query(Order).count() == 0
    assert any("missing in Cheape Deal" in m for m in notifier.sent)


def test_reconcile_flags_order_without_supplier_record(db, rt, product, notifier):
    order, *_ = O.intake(db, make_norm("302"))
    order.status = SUBMITTED  # simulate corruption
    db.commit()
    rep = Y.reconcile(db, rt)
    assert any("no supplier order record" in p for p in rep["problems"])


def test_alert_dedupe_prevents_telegram_spam(db, rt, product, notifier):
    for _ in range(5):
        rt.alert(db, level="error", source="x", message="same problem", dedupe_key="k1")
    db.commit()
    assert db.query(Alert).count() == 1 and len(notifier.sent) == 1
