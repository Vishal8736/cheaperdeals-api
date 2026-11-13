from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.adapters.base import SupplierError
from app.db import utcnow
from app.models import (
    ALLOWED_TRANSITIONS, AuditLog, CANCELLED, FAILED_MANUAL, FAILED_RETRY, IllegalTransition, Order, PENDING_APPROVAL,
    SHIPPED, SUBMITTED, SupplierOrder, DELIVERED,
)
from app.services import orders as O
from app.services import settings_service as S
from tests.conftest import make_norm


def ingest(db, norm):
    order, created, reason = O.intake(db, norm)
    db.commit()
    return order, created, reason


# ---- idempotency ---------------------------------------------------------------------------
def test_duplicate_webhook_creates_one_order(db, product):
    o1, c1, _ = ingest(db, make_norm("42"))
    o2, c2, reason = ingest(db, make_norm("42"))
    assert c1 is True and c2 is False and reason == "duplicate"
    assert o1.id == o2.id
    assert db.query(Order).count() == 1


def test_unpaid_order_is_ignored(db, product):
    order, created, reason = ingest(db, make_norm("43", paid=False, status="pending"))
    assert order is None and not created and "not paid" in reason


def test_double_processing_places_one_supplier_order(db, rt, supplier, product, live):
    order, *_ = ingest(db, make_norm("44"))
    r1 = O.process_order(db, rt, order.id)
    r2 = O.process_order(db, rt, order.id)
    assert r1["outcome"] == "submitted" and r2["outcome"] == "noop"
    assert supplier.calls.count("create_order") == 1


def test_lock_blocks_concurrent_claim(db, product):
    order, *_ = ingest(db, make_norm("45"))
    t1 = O.claim(db, order.id)
    assert t1 and O.claim(db, order.id) is None
    O.release(db, order.id, t1)
    assert O.claim(db, order.id)


# ---- state machine -------------------------------------------------------------------------
def test_illegal_transition_rejected(db, product):
    order, *_ = ingest(db, make_norm("46"))
    with pytest.raises(IllegalTransition):
        O.transition(db, order, DELIVERED, action="x")
    assert order.status == "received"


def test_terminal_states_have_no_exits():
    assert ALLOWED_TRANSITIONS[DELIVERED] == set() and ALLOWED_TRANSITIONS[CANCELLED] == set()


def test_every_state_change_is_audited(db, rt, product, live):
    order, *_ = ingest(db, make_norm("47"))
    O.process_order(db, rt, order.id)
    actions = [a.action for a in db.query(AuditLog).filter_by(entity_id=order.id).order_by(AuditLog.id)]
    assert actions == ["received", "validated", "submitted"]


# ---- dry run / kill switch -----------------------------------------------------------------
def test_dry_run_never_calls_supplier(db, rt, supplier, product):
    assert S.get_bool(db, "dry_run") is True  # safe default
    order, *_ = ingest(db, make_norm("50"))
    res = O.process_order(db, rt, order.id)
    assert res["outcome"] == "dry_run_submitted"
    assert "create_order" not in supplier.calls
    assert db.get(Order, order.id).supplier_order.dry_run is True


def test_kill_switch_stops_everything(db, rt, supplier, product, live):
    order, *_ = ingest(db, make_norm("51"))
    S.set_value(db, "paused", "true")
    db.commit()
    assert O.process_order(db, rt, order.id)["outcome"] == "paused"
    assert O.process_pending(db, rt)["paused"] is True
    assert supplier.calls == []


# ---- validation guards ---------------------------------------------------------------------
def test_negative_margin_is_blocked(db, rt, supplier, product, live):
    order, *_ = ingest(db, make_norm("60", unit="9.00"))  # cost 10.00, paid 9.00
    res = O.process_order(db, rt, order.id)
    assert res["outcome"] == "failed_manual" and "margin" in res["error"]
    assert supplier.calls == []


def test_margin_below_minimum_is_blocked(db, rt, supplier, product, live):
    order, *_ = ingest(db, make_norm("61", unit="11.00"))  # ~9% margin < 20% default
    assert O.process_order(db, rt, order.id)["outcome"] == "failed_manual"


def test_product_margin_override_wins_over_global(db, rt, supplier, product, live):
    product.min_margin_pct = Decimal("5")
    db.commit()
    order, *_ = ingest(db, make_norm("62", unit="11.00"))
    assert O.process_order(db, rt, order.id)["outcome"] == "submitted"


def test_out_of_stock_is_blocked(db, rt, supplier, product, live):
    product.stock = 0
    db.commit()
    order, *_ = ingest(db, make_norm("63"))
    res = O.process_order(db, rt, order.id)
    assert res["outcome"] == "failed_manual" and "out of stock" in res["error"]


def test_unknown_product_is_blocked(db, rt, supplier, live):
    order, *_ = ingest(db, make_norm("64", pid="NOPE"))
    res = O.process_order(db, rt, order.id)
    assert res["outcome"] == "failed_manual" and "not in Products" in res["error"]


def test_live_requires_shipping_method(db, rt, product):
    S.set_value(db, "dry_run", "false")
    db.commit()
    order, *_ = ingest(db, make_norm("65"))
    res = O.process_order(db, rt, order.id)
    assert res["outcome"] == "failed_manual" and "shipping method" in res["error"]


# ---- approval ------------------------------------------------------------------------------
def test_approval_threshold_holds_order_then_approve_submits(db, rt, supplier, product, live):
    S.set_value(db, "approval_threshold", "100")
    db.commit()
    order, *_ = ingest(db, make_norm("70", qty=5))  # total 125
    assert O.process_order(db, rt, order.id)["outcome"] == "needs_approval"
    assert db.get(Order, order.id).status == PENDING_APPROVAL
    assert supplier.calls == []
    assert O.process_order(db, rt, order.id)["outcome"] == "awaiting_approval"
    assert O.approve(db, rt, order.id)["outcome"] == "submitted"
    assert supplier.calls.count("create_order") == 1


def test_reject_cancels_order(db, rt, supplier, product, live):
    S.set_value(db, "approval_threshold", "50")
    db.commit()
    order, *_ = ingest(db, make_norm("71", qty=3))
    O.process_order(db, rt, order.id)
    assert O.reject(db, rt, order.id)["outcome"] == "cancelled"
    assert db.get(Order, order.id).status == CANCELLED and supplier.calls == []


def test_small_order_skips_approval(db, rt, supplier, product, live):
    S.set_value(db, "approval_threshold", "100")
    db.commit()
    order, *_ = ingest(db, make_norm("72"))
    assert O.process_order(db, rt, order.id)["outcome"] == "submitted"


# ---- daily cap -----------------------------------------------------------------------------
def test_daily_cap_defers_extra_orders(db, rt, supplier, product, live):
    S.set_value(db, "daily_order_cap", "1")
    db.commit()
    a, *_ = ingest(db, make_norm("80"))
    b, *_ = ingest(db, make_norm("81"))
    assert O.process_order(db, rt, a.id)["outcome"] == "submitted"
    assert O.process_order(db, rt, b.id)["outcome"] == "daily_cap"
    assert db.get(Order, b.id).status == "validated"  # waits, not failed
    assert supplier.calls.count("create_order") == 1


# ---- supplier failures ---------------------------------------------------------------------
def test_supplier_429_schedules_retry_with_backoff(db, rt, supplier, product, live):
    supplier.fail_next = [SupplierError("rate limited", retryable=True, status_code=429)]
    order, *_ = ingest(db, make_norm("90"))
    res = O.process_order(db, rt, order.id)
    o = db.get(Order, order.id)
    assert res["outcome"] == "retry_scheduled" and res["in_minutes"] == 2
    assert o.status == FAILED_RETRY and o.retry_count == 1 and o.next_retry_at > utcnow()
    assert o.supplier_order is None  # failed attempt leaves no phantom supplier order
    # not due yet -> sweeper leaves it alone
    assert O.process_order(db, rt, order.id)["outcome"] == "not_due"
    # due -> succeeds
    o.next_retry_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert O.process_order(db, rt, order.id)["outcome"] == "submitted"


def test_backoff_doubles_and_exhausts_to_manual(db, rt, supplier, product, live):
    S.set_value(db, "max_retries", "2")
    db.commit()
    supplier.fail_next = [SupplierError("429", retryable=True) for _ in range(5)]
    order, *_ = ingest(db, make_norm("91"))
    delays = []
    for _ in range(3):
        res = O.process_order(db, rt, order.id)
        delays.append(res.get("in_minutes"))
        o = db.get(Order, order.id)
        o.next_retry_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert delays[:2] == [2, 4]
    assert db.get(Order, order.id).status == FAILED_MANUAL
    assert "Retries exhausted" in db.get(Order, order.id).last_error


def test_permanent_supplier_error_goes_manual_without_retry(db, rt, supplier, product, live):
    supplier.fail_next = [SupplierError("invalid address")]
    order, *_ = ingest(db, make_norm("92"))
    assert O.process_order(db, rt, order.id)["outcome"] == "failed_manual"
    assert supplier.calls.count("create_order") == 1


def test_timeout_after_send_with_lookup_finds_order_no_duplicate(db, rt, supplier, product, live):
    supplier.lose_response_next = 1  # supplier created the order but we never saw the reply
    order, *_ = ingest(db, make_norm("93"))
    res = O.process_order(db, rt, order.id)
    assert res["outcome"] == "submitted"
    assert supplier.calls.count("create_order") == 1  # NOT placed twice
    assert db.get(Order, order.id).supplier_order.supplier_order_id.startswith("MOCK-")


def test_timeout_without_lookup_stops_for_human(db, rt, supplier, notifier, product, live):
    supplier.supports_lookup = False
    supplier.lose_response_next = 1
    order, *_ = ingest(db, make_norm("94"))
    res = O.process_order(db, rt, order.id)
    assert res["outcome"] == "failed_manual"
    assert O.process_order(db, rt, order.id)["outcome"] == "noop"
    assert O.retry(db, rt, order.id)["outcome"] == "needs_resolution"  # cannot blindly retry
    assert supplier.calls.count("create_order") == 1
    assert any("Check the supplier dashboard" in m for m in notifier.sent)


def test_operator_resolves_ambiguous_as_placed(db, rt, supplier, product, live):
    supplier.supports_lookup = False
    supplier.lose_response_next = 1
    order, *_ = ingest(db, make_norm("95"))
    O.process_order(db, rt, order.id)
    res = O.resolve_ambiguous(db, rt, order.id, supplier_order_id="CJ-777")
    o = db.get(Order, order.id)
    assert res["outcome"] == "submitted" and o.status == SUBMITTED and o.supplier_order.supplier_order_id == "CJ-777"


def test_operator_resolves_ambiguous_as_not_placed_then_places(db, rt, supplier, product, live):
    supplier.supports_lookup = False
    supplier.lose_response_next = 1
    order, *_ = ingest(db, make_norm("96"))
    O.process_order(db, rt, order.id)
    supplier.by_number.clear()  # operator checked: nothing there
    res = O.resolve_ambiguous(db, rt, order.id, supplier_order_id=None)
    assert res["outcome"] == "submitted"
    assert supplier.calls.count("create_order") == 2  # a deliberate second attempt


def test_operator_retry_after_fixing_stock(db, rt, supplier, product, live):
    product.stock = 0
    db.commit()
    order, *_ = ingest(db, make_norm("97"))
    assert O.process_order(db, rt, order.id)["outcome"] == "failed_manual"
    product.stock = 10
    db.commit()
    assert O.retry(db, rt, order.id)["outcome"] == "submitted"


def test_cancel_warns_when_supplier_order_exists(db, rt, supplier, product, live):
    order, *_ = ingest(db, make_norm("98"))
    O.process_order(db, rt, order.id)
    res = O.cancel(db, rt, order.id)
    assert res["supplier_order_placed"] is True and db.get(Order, order.id).status == CANCELLED


def test_redo_live_after_dry_run(db, rt, supplier, product):
    order, *_ = ingest(db, make_norm("99"))
    O.process_order(db, rt, order.id)  # dry run
    assert O.redo_live(db, rt, order.id)["outcome"] == "still_dry_run"
    S.set_value(db, "dry_run", "false")
    S.set_value(db, "default_logistic_name", "TestPost")
    db.commit()
    assert O.redo_live(db, rt, order.id)["outcome"] == "submitted"
    assert supplier.calls.count("create_order") == 1
    assert db.get(Order, order.id).supplier_order.dry_run is False


def test_process_pending_picks_up_new_and_due_orders(db, rt, supplier, product, live):
    a, *_ = ingest(db, make_norm("100"))
    supplier.fail_next = [SupplierError("429", retryable=True)]
    b, *_ = ingest(db, make_norm("101"))
    res = O.process_pending(db, rt)
    assert res["processed"] == 2
    ob = db.get(Order, b.id) if False else db.get(Order, a.id)  # first order hit the 429
    assert {db.get(Order, a.id).status, db.get(Order, b.id).status} == {FAILED_RETRY, SUBMITTED}
