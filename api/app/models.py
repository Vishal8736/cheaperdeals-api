from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# ---- Order state machine -------------------------------------------------------------------
RECEIVED = "received"
VALIDATED = "validated"
PENDING_APPROVAL = "pending_approval"
SUBMITTED = "submitted_to_supplier"
CONFIRMED = "supplier_confirmed"
SHIPPED = "shipped"
DELIVERED = "delivered"
FAILED_RETRY = "failed_retry"
FAILED_MANUAL = "failed_manual"
CANCELLED = "cancelled"

PIPELINE_STAGES = [RECEIVED, VALIDATED, PENDING_APPROVAL, SUBMITTED, CONFIRMED, SHIPPED, DELIVERED]
ALL_STATES = PIPELINE_STAGES + [FAILED_RETRY, FAILED_MANUAL, CANCELLED]
TERMINAL_STATES = {DELIVERED, CANCELLED}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    RECEIVED: {VALIDATED, PENDING_APPROVAL, FAILED_MANUAL, CANCELLED},
    VALIDATED: {PENDING_APPROVAL, SUBMITTED, FAILED_RETRY, FAILED_MANUAL, CANCELLED},
    PENDING_APPROVAL: {VALIDATED, SUBMITTED, FAILED_RETRY, FAILED_MANUAL, CANCELLED},
    SUBMITTED: {CONFIRMED, SHIPPED, DELIVERED, VALIDATED, FAILED_MANUAL, CANCELLED},  # VALIDATED only via redo_live()
    CONFIRMED: {SHIPPED, DELIVERED, FAILED_MANUAL, CANCELLED},
    SHIPPED: {DELIVERED, FAILED_MANUAL},
    DELIVERED: set(),
    FAILED_RETRY: {VALIDATED, SUBMITTED, FAILED_RETRY, FAILED_MANUAL, CANCELLED},
    FAILED_MANUAL: {VALIDATED, CANCELLED},  # operator retry goes back to validated
    CANCELLED: set(),
}


class IllegalTransition(Exception):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_product_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    sku: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(255))
    supplier_vid: Mapped[str] = mapped_column(String(128), default="")
    supplier_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    selling_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    min_margin_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2), nullable=True)  # None = global
    stock: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # operator switch
    auto_hidden: Mapped[bool] = mapped_column(Boolean, default=False)  # system switch (stock/margin)
    manual_override: Mapped[bool] = mapped_column(Boolean, default=False)  # never auto-reprice
    hidden_reason: Mapped[str] = mapped_column(String(255), default="")
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    # catalog fields (added in v2). Existing rows default to "listed" so nothing changes for them.
    supplier_connection_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    supplier_product_id: Mapped[str] = mapped_column(String(128), default="")
    image_url: Mapped[str] = mapped_column(String(600), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(128), default="")
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | import
    listing_status: Mapped[str] = mapped_column(String(16), default="listed")  # draft | listed
    listed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    backup_connection_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None + backup_vid set = built-in supplier
    backup_vid: Mapped[str] = mapped_column(String(128), default="")  # empty = no backup supplier
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(8, 3), default=Decimal("0"))  # 0 = unknown: the country's flat shipping is used
    search_query: Mapped[str] = mapped_column(String(200), default="")  # words the price feed searches for (e.g. "table fan 16 inch")
    cost_currency: Mapped[str] = mapped_column(String(3), default="")  # currency the supplier quotes in; "" = USD (the base currency)
    strategy_managed: Mapped[bool] = mapped_column(Boolean, default=False)  # price is set by the Strategy agent, not by the markup rule
    target_market_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # market the strategy chose to sell this in
    playbook_base: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)  # price before the Playbook's demand/clearance/sale change
    playbook_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)  # the price the Playbook last set (to notice other changes)
    playbook_tag: Mapped[str] = mapped_column(String(120), default="")  # what the Playbook is doing to this product right now
    playbook_since: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # when the Playbook first saw the product (age for slow sellers)
    # v18: what kind of product this is. dropship = we buy from a supplier after the sale; pod = print-on-demand supplier prints and ships (same flow);
    # affiliate = we only show it and send the visitor to the merchant with our tracked link (no checkout, no stock, the merchant pays a commission)
    kind: Mapped[str] = mapped_column(String(12), default="dropship", index=True)
    affiliate_url: Mapped[str] = mapped_column(String(1500), default="")  # the merchant / network link (affiliate products only)
    merchant: Mapped[str] = mapped_column(String(80), default="")  # who sells it, shown as "Buy at <merchant>"
    commission_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)  # the commission the program/feed states (may be None)
    clicks: Mapped[int] = mapped_column(Integer, default=0)  # visitors sent to the merchant through /shop/go
    last_click_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    listings: Mapped[list["ProductListing"]] = relationship(back_populates="product", cascade="all, delete-orphan")

    def margin_pct(self, selling: Decimal | None = None) -> Decimal:
        sp = Decimal(selling if selling is not None else self.selling_price)
        if sp <= 0:
            return Decimal("-100")
        return ((sp - Decimal(self.supplier_price)) / sp * 100).quantize(Decimal("0.01"))


class PriceHistory(Base):
    __tablename__ = "price_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    supplier_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    selling_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # idempotency key
    store_order_number: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default=RECEIVED, index=True)
    customer_name: Mapped[str] = mapped_column(String(255), default="")
    customer_email: Mapped[str] = mapped_column(String(255), default="")
    customer_phone: Mapped[str] = mapped_column(String(64), default="")
    ship_address1: Mapped[str] = mapped_column(String(255), default="")
    ship_address2: Mapped[str] = mapped_column(String(255), default="")
    ship_city: Mapped[str] = mapped_column(String(128), default="")
    ship_state: Mapped[str] = mapped_column(String(128), default="")
    ship_zip: Mapped[str] = mapped_column(String(32), default="")
    ship_country_code: Mapped[str] = mapped_column(String(8), default="")
    ship_country: Mapped[str] = mapped_column(String(128), default="")
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    lock_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="webhook")  # webhook | sweep | dev
    store_connection_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None = the built-in store
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    risk_reasons: Mapped[str] = mapped_column(Text, default="")
    ship_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))  # what getting this parcel to the customer costs us
    ship_cost_actual: Mapped[bool] = mapped_column(Boolean, default=False)  # False = our estimate, True = typed in by you
    public_token: Mapped[str] = mapped_column(String(32), default="", index=True)  # for the customer's /track/<token> page
    note: Mapped[str] = mapped_column(Text, default="")  # internal note, never shown to the customer
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id")
    supplier_order: Mapped[Optional["SupplierOrder"]] = relationship(back_populates="order", uselist=False, cascade="all, delete-orphan")
    tracking: Mapped[list["TrackingEvent"]] = relationship(back_populates="order", cascade="all, delete-orphan", order_by="TrackingEvent.id")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="order", cascade="all, delete-orphan", order_by="Approval.id")


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))  # supplier cost frozen when the order arrived (0 = unknown)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"), nullable=True)
    store_product_id: Mapped[str] = mapped_column(String(64), default="")
    sku: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Optional[Product]] = relationship()


class SupplierOrder(Base):
    __tablename__ = "supplier_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), unique=True)
    order_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # what we send as orderNumber
    supplier_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(16), default="attempting")  # attempting | created | dry_run
    supplier_status: Mapped[str] = mapped_column(String(64), default="")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    request_json: Mapped[str] = mapped_column(Text, default="")
    response_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    order: Mapped[Order] = relationship(back_populates="supplier_order")


class TrackingEvent(Base):
    __tablename__ = "tracking_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    tracking_number: Mapped[str] = mapped_column(String(128), default="")
    carrier: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    customer_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    emailed: Mapped[bool] = mapped_column(Boolean, default=False)  # the customer e-mail for this event was sent (or deliberately skipped)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    order: Mapped[Order] = relationship(back_populates="tracking")


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(16), default="error")  # info | warning | error
    source: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | rejected
    reason: Mapped[str] = mapped_column(String(255), default="")
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    order: Mapped[Order] = relationship(back_populates="approvals")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column(String(32), default="order")
    entity_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    action: Mapped[str] = mapped_column(String(64))
    from_state: Mapped[str] = mapped_column(String(32), default="")
    to_state: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class EmailLog(Base):
    """Every email the app sent (or tried to send) from the connected mailbox: by you, by the AI, or automatically."""
    __tablename__ = "email_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    to_addr: Mapped[str] = mapped_column(String(320), default="")
    subject: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    by: Mapped[str] = mapped_column(String(32), default="you")  # you | ai | system
    ok: Mapped[bool] = mapped_column(default=False)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ShopOrder(Base):
    """An order placed on the public website. It becomes a normal Order (and the engine takes over) only once it is paid."""
    __tablename__ = "shop_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # customer's secret link: /track/<token>
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | paid | cancelled | refunded
    pay_mode: Mapped[str] = mapped_column(String(16), default="manual")  # manual | razorpay
    customer_name: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    address1: Mapped[str] = mapped_column(String(255), default="")
    address2: Mapped[str] = mapped_column(String(255), default="")
    city: Mapped[str] = mapped_column(String(128), default="")
    state: Mapped[str] = mapped_column(String(128), default="")
    zip_code: Mapped[str] = mapped_column(String(32), default="")
    country_code: Mapped[str] = mapped_column(String(8), default="")
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    shipping: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    pay_ref: Mapped[str] = mapped_column(String(128), default="", index=True)  # gateway order / invoice id
    pay_data: Mapped[str] = mapped_column(Text, default="{}")  # what the customer needs to pay (UPI link, wallet offers, BTCPay link...)
    total_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))  # what the engine books (it works in USD)
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("1"))
    payment_id: Mapped[str] = mapped_column(String(128), default="")
    engine_order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # orders.id once paid
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Subscriber(Base):
    """E-mail addresses collected by the website's 'deal alerts' box."""
    __tablename__ = "subscribers"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


Index("ix_orders_status_retry", Order.status, Order.next_retry_at)


# ---- connectors, listings, agents (v2) -----------------------------------------------------
class Connection(Base):
    """One connected company (supplier) or website (store). Secrets are encrypted at rest (see secretbox.py)."""
    __tablename__ = "connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # supplier | store
    type: Mapped[str] = mapped_column(String(64))  # registry key, e.g. cj, generic_rest, file:acme
    name: Mapped[str] = mapped_column(String(120))
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    secrets_enc: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    last_status: Mapped[str] = mapped_column(String(16), default="untested")  # untested | ok | error
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_tested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProductListing(Base):
    """A product published on one store / website."""
    __tablename__ = "product_listings"
    __table_args__ = (UniqueConstraint("product_id", "connection_id", name="uq_listing_product_conn"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[int] = mapped_column(Integer, default=0)  # 0 = the built-in store
    store_label: Mapped[str] = mapped_column(String(120), default="")
    remote_id: Mapped[str] = mapped_column(String(128), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(16), default="listed")  # listed | error
    price_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)  # own price for this store (multi-country); None = follows the product price
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    product: Mapped[Product] = relationship(back_populates="listings")


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(12), default="running")  # running | ok | error
    trigger: Mapped[str] = mapped_column(String(16), default="scheduler")  # scheduler | n8n | manual | event
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(String(500), default="")
    error: Mapped[str] = mapped_column(Text, default="")


class AgentEvent(Base):
    """One line of the live activity feed."""
    __tablename__ = "agent_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(32), index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")  # info | ok | warn | error
    message: Mapped[str] = mapped_column(String(500), default="")
    order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class FxRate(Base):
    """Units of a currency per 1 USD. USD is the base currency of the whole system (supplier cost, selling price, profit)."""
    __tablename__ = "fx_rates"
    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    per_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    source: Mapped[str] = mapped_column(String(12), default="seed")  # seed (approximate, unconfirmed) | manual | auto
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Market(Base):
    """A country you can sell into, with what it costs to get a parcel there."""
    __tablename__ = "markets"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True)  # US, GB, DE ...
    name: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    shipping_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))  # base shipping cost per parcel to this country
    ship_per_kg_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))  # extra per kg when the product weight is known
    duty_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))  # import duty on (product + shipping)
    tax_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))  # VAT/GST included in shelf prices there
    fee_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("3"))  # payment gateway fee
    store_connection_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # store that sells into this market (None = main store)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class MarketPrice(Base):
    """What the same (or a comparable) product sells for in a market. Entered by you, imported by CSV, or pushed by a scraper."""
    __tablename__ = "market_prices"
    __table_args__ = (UniqueConstraint("product_id", "market_id", name="uq_marketprice_product_market"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    local_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))  # shelf price in that market's currency, tax included
    url: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(8), default="manual")  # manual | csv | api (api is treated as untrusted when the gap looks absurd)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StrategyRec(Base):
    """The latest verdict of the Strategy agent for one product."""
    __tablename__ = "strategy_recs"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), unique=True, index=True)
    market_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(14), default="no_data")  # viable | applied | not_viable | needs_check | needs_rate | stale | no_data
    price_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    price_local: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    reference_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    profit_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    profit_pct: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("0"))
    reason: Mapped[str] = mapped_column(String(300), default="")
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SourceOffer(Base):
    """Something you can BUY cheaply on a B2B marketplace or from a local seller (IndiaMART, Alibaba, TradeIndia, a shop...)."""
    __tablename__ = "source_offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    source_name: Mapped[str] = mapped_column(String(80), default="")  # IndiaMART, Alibaba, ...
    url: Mapped[str] = mapped_column(String(600), default="")
    image_url: Mapped[str] = mapped_column(String(600), default="")
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    moq: Mapped[int] = mapped_column(Integer, default=1)  # minimum order quantity the seller demands
    available_qty: Mapped[int] = mapped_column(Integer, default=100)  # what the seller says he has (ask him)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(8, 3), default=Decimal("0"))
    origin_country: Mapped[str] = mapped_column(String(8), default="IN")
    seller_name: Mapped[str] = mapped_column(String(120), default="")
    seller_email: Mapped[str] = mapped_column(String(200), default="")
    seller_phone: Mapped[str] = mapped_column(String(40), default="")
    search_query: Mapped[str] = mapped_column(String(200), default="")
    single_ok: Mapped[bool] = mapped_column(Boolean, default=False)  # the seller agreed to ship single units (needed when MOQ > 1)
    category: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(10), default="new", index=True)  # new | imported | ignored
    product_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class OfferTarget(Base):
    """What the same kind of product sells for in a country (before you have decided to import the offer)."""
    __tablename__ = "offer_targets"
    __table_args__ = (UniqueConstraint("offer_id", "market_id", name="uq_offertarget"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("source_offers.id", ondelete="CASCADE"), index=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    local_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    url: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(8), default="manual")
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReturnCase(Base):
    """A customer return / refund request and what it cost you."""
    __tablename__ = "returns"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(12), default="requested", index=True)  # requested | approved | rejected | received | refunded
    reason: Mapped[str] = mapped_column(String(300), default="")
    refund_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))  # what you gave back to the customer
    supplier_refund_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))  # what the supplier gave back to you
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    order: Mapped[Order] = relationship()


class AppUser(Base):
    """Extra people who can sign in. The first admin comes from the settings file; these are added on the Users page."""
    __tablename__ = "app_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10), default="viewer")  # operator | viewer
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Ticket(Base):
    """A customer message from the website: an order problem, a question, a return request or plain feedback."""
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # the customer's secret link: /#ticket=<token>
    kind: Mapped[str] = mapped_column(String(16), default="issue")  # issue | question | return | feedback
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open | answered | closed
    name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(254), default="", index=True)
    subject: Mapped[str] = mapped_column(String(200), default="")
    order_number: Mapped[str] = mapped_column(String(32), default="")
    shop_order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # set when the order number + email matched a real order
    rating: Mapped[int] = mapped_column(Integer, default=0)  # 0 = none, else 1-5 (feedback)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    messages: Mapped[list["TicketMessage"]] = relationship(order_by="TicketMessage.id", cascade="all, delete-orphan")


class TicketMessage(Base):
    __tablename__ = "ticket_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    who: Mapped[str] = mapped_column(String(10), default="customer")  # customer | admin
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Review(Base):
    """A product review from someone who really bought it. Shown only after you approve it."""
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    shop_order_id: Mapped[int] = mapped_column(Integer, index=True)
    rating: Mapped[int] = mapped_column(Integer, default=5)
    name: Mapped[str] = mapped_column(String(60), default="")
    title: Mapped[str] = mapped_column(String(120), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)  # pending | approved | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DiagEvent(Base):
    """One log line worth keeping (INFO and up from our own code, WARNING and up from libraries). Secrets are removed before saving."""
    __tablename__ = "diag_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="INFO", index=True)
    category: Mapped[str] = mapped_column(String(24), default="app", index=True)
    logger: Mapped[str] = mapped_column(String(80), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    exc_type: Mapped[str] = mapped_column(String(120), default="")
    trace: Mapped[str] = mapped_column(Text, default="")
    fingerprint: Mapped[str] = mapped_column(String(16), default="", index=True)  # same problem => same fingerprint
    ref: Mapped[str] = mapped_column(String(12), default="", index=True)  # the "Error ID" shown to the person (also X-Request-ID)
    path: Mapped[str] = mapped_column(String(200), default="")


class Click(Base):
    """One visitor sent to a merchant through /shop/go (v19). Kept ~120 days; lets the dashboard show clicks today / this week and earnings per click."""
    __tablename__ = "clicks"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Earning(Base):
    """One commission the affiliate NETWORK reports (v19), pulled through its API. We never make these up: a row exists only if the network returned it.
    amount_usd is None when the currency has no exchange rate yet."""
    __tablename__ = "earnings"
    __table_args__ = (UniqueConstraint("network", "tx_id", name="uq_earning_network_tx"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    network: Mapped[str] = mapped_column(String(32), index=True)  # aff_awin | aff_impact | aff_cj_affiliate
    connection_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    tx_id: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending | approved | paid | declined
    merchant: Mapped[str] = mapped_column(String(120), default="")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))  # commission, in `currency`
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    amount_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4), nullable=True)
    sale_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sub: Mapped[str] = mapped_column(String(80), default="")  # the sub-id we put on the click (p<product id>), when the network returns it
    product_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Design(Base):
    """A print design made by the POD agent (v20): code-rendered typography/pattern art (not photos, not copied from anyone). The PNG lives in the DB so the
    Render copy can serve it to Printful (which fetches designs from a public URL)."""
    __tablename__ = "designs"
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    niche: Mapped[str] = mapped_column(String(80), default="", index=True)
    text: Mapped[str] = mapped_column(String(160), default="")
    style: Mapped[str] = mapped_column(String(24), default="stack")
    palette: Mapped[str] = mapped_column(String(24), default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    png: Mapped[bytes] = mapped_column(LargeBinary)
    source: Mapped[str] = mapped_column(String(16), default="builtin")  # builtin slogan bank | ai
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PodItem(Base):
    """One design put on one product type at one POD supplier."""
    __tablename__ = "pod_items"
    __table_args__ = (UniqueConstraint("design_id", "connection_id", "ptype", name="uq_pod_item"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    design_id: Mapped[int] = mapped_column(Integer, index=True)
    connection_id: Mapped[int] = mapped_column(Integer, index=True)
    ptype: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending | created | listed | error
    provider_ref: Mapped[str] = mapped_column(String(80), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    error: Mapped[str] = mapped_column(String(400), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
