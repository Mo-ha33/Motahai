"""
db.py — Persistence layer (SQLAlchemy 2.0) for the Motahai conversion pipeline (S1-1).

Works on SQLite (dev/tests) and Postgres (prod): only portable column types and constraints are used.
Engine URL comes from env DATABASE_URL (default sqlite:///./motahai.db), read lazily.

Privacy: customer email/phone are stored ONLY as SHA-256 hashes (orders.email_hash / phone_hash) and
webhook payloads are never stored in plaintext: webhook_deliveries keeps only their SHA-256, and staged_webhooks keeps
a Fernet-encrypted copy only until the webhook is processed (see webhook_staging.py). Credentials live encrypted
(see credentials.py).

All timestamps are timezone-aware UTC. SQLite drops tzinfo on read, so UTCDateTime restores it.

Schema is created with create_all() for now. NOTE: create_all() never ALTERs an existing table, so the S2 columns
(orders match-key hashes + delivered_at, capi_events due_at/claimed_at + wider status CHECK, tenants settlement_hours/
storefront_url, FX-3 tenants.confirmation_rules + orders.confirmation_source, S1-4 tenants.digest_email +
tenants.language, tenants.meta_test_event_code) and the new checkout_context and staged_webhooks tables need an Alembic
migration before any non-empty database is upgraded.
TODO(follow-up): move to Alembic migrations before the first production schema change.
"""

import os
import re
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, create_engine, event, select
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

DATABASE_URL_ENV = "DATABASE_URL"
DEFAULT_DATABASE_URL = "sqlite:///./motahai.db"

PLATFORMS = ("shopify", "salla", "zid")
TENANT_MODES = ("shadow", "live")
# scheduled = eligible, waiting for its settlement window (due_at); late_delivery = past the placed+6.5d cutoff, never sent.
CAPI_STATUSES = ("pending", "scheduled", "sent", "failed", "shadow", "stale", "late_delivery")
STATUS_SOURCES = ("webhook", "sweep", "manual")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetime that round-trips on SQLite (which stores naive values)."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: Optional[datetime], dialect) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("mode IN ('shadow', 'live')", name="ck_tenants_mode"),
        CheckConstraint("platform IN ('shopify', 'salla', 'zid')", name="ck_tenants_platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    platform: Mapped[str] = mapped_column(String(16))
    shop_domain: Mapped[str] = mapped_column(String(255), unique=True)
    meta_dataset_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    country: Mapped[str] = mapped_column(String(2), default="EG")
    currency: Mapped[str] = mapped_column(String(3), default="EGP")
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Cairo")
    mode: Mapped[str] = mapped_column(String(8), default="shadow")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # S2-1: hours between an order becoming delivered/paid and its CAPI send (NULL = order_pipeline default, 12h).
    settlement_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # S2-3: public storefront URL for event_source_url (NULL: Shopify falls back to https://<shop_domain>/).
    storefront_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # FX-3: per-tenant ConfirmedOrder rules {"tags": [...], "statuses": [...], "implicit_on_ship": bool}; NULL = defaults.
    # Read via confirmation.effective_confirmation_rules, write via confirmation.set_confirmation_rules (validated).
    confirmation_rules: Mapped[Optional[dict]] = mapped_column(JSON(none_as_null=True), nullable=True)
    # S1-4: weekly Signal Hygiene digest recipient (NULL = no digest) and its language ('ar' default, or 'en').
    digest_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="ar")
    # Meta Events Manager test code. When set, every pipeline CAPI send for this tenant carries `test_event_code`, so a
    # live tenant's events show in Test Events. Test sends still consume the (tenant, order, event) idempotency claim,
    # so use it on a test dataset or before launch, then clear it. NULL = normal sends. Set via set_meta_test_event_code.
    meta_test_event_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    def __repr__(self) -> str:
        return f"Tenant(id={self.id!r}, shop_domain={self.shop_domain!r}, mode={self.mode!r})"


class Credential(Base):
    """Fernet-encrypted secret. Use credentials.store_credential / get_credential; never read ciphertext directly."""
    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", name="uq_credentials_tenant_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    rotated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)

    def __repr__(self) -> str:
        # Deliberately omits ciphertext: a repr must never carry secret material.
        return f"Credential(id={self.id!r}, tenant_id={self.tenant_id!r}, kind={self.kind!r})"


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("tenant_id", "platform_order_id", name="uq_orders_tenant_platform_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    platform_order_id: Mapped[str] = mapped_column(String(64))
    is_cod: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(3))
    current_status: Mapped[str] = mapped_column(String(32))
    created_at_platform: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    # Attribution (S1-3 fills these; the order upsert never wipes them)
    utm_source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    utm_medium: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    utm_campaign: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    utm_content: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ad_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    fbp: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    fbc: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ttclid: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    sccid: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Customer identifiers: SHA-256 hex only. Raw email/phone are never stored.
    email_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    phone_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # S2-3 match keys, SHA-256 hex only (Meta-normalized before hashing). Column name is f"{key}_hash".
    external_id_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    fn_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ln_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ct_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    st_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    zp_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    country_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # S2-1: when we FIRST saw the order delivered (COD) / paid (prepaid). Starts the settlement window.
    delivered_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    # S2-2: when the customer confirmed the order (call, WhatsApp, or merchant tag 'confirmed')
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    # FX-3: why the order counts as confirmed: tag | status | implicit_shipped | implicit_paid | manual (see confirmation.py)
    confirmation_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # S2-2: full order total before refunds (for ConfirmedOrder value)
    order_total: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class OrderStatusEvent(Base):
    __tablename__ = "order_status_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16), default="webhook")  # webhook | sweep | manual
    topic: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    payload_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class CapiEvent(Base):
    """
    One row per (tenant, order, event_name): the UNIQUE constraint IS the idempotency key. Inserting the row
    (status pending) claims the right to send; a constraint violation means it was already emitted.
    """
    __tablename__ = "capi_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "order_id", "event_name", name="uq_capi_events_idempotency"),
        CheckConstraint(
            "status IN ('pending', 'scheduled', 'sent', 'failed', 'shadow', 'stale', 'late_delivery')",
            name="ck_capi_events_status"),
        Index("ix_capi_events_status_created", "status", "created_at"),  # retry sweep
        Index("ix_capi_events_status_due", "status", "due_at"),  # send_due_events
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    event_name: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(128))
    event_time: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # unix seconds sent to Meta
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    http_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fbtrace_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    sent_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    # S2-1: a `scheduled` row is sent once due_at has passed.
    due_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    # When a scheduled/failed row was last claimed for sending (orphan detection uses this, not created_at).
    claimed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    # Comma-separated send-time data-quality flags, e.g. "missing_user_agent,missing_event_source_url" (see capi_service).
    quality_flags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CheckoutContext(Base):
    """
    D-006: the checkout client IP and user agent, Fernet-encrypted (credentials.encrypt_value), kept only until the
    conversion is successfully sent live or expires_at (captured_at + 14 days), whichever comes first. Never plaintext.
    """
    __tablename__ = "checkout_context"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    ip_ciphertext: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ua_ciphertext: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)

    def __repr__(self) -> str:
        return f"CheckoutContext(id={self.id!r}, order_id={self.order_id!r})"  # no ciphertext in reprs


class PendingCapture(Base):
    """
    FX-2: a storefront thank-you-page capture, QUARANTINED until the signed platform webhook creates the order.
    The public capture endpoint only ever writes here (never to orders). capture.merge_pending_captures later joins a
    row to the order with the same (tenant, platform_order_id) after an order-total second-factor check. IP/UA are
    Fernet ciphertext (D-006), cleared once the row is merged/rejected; the row itself is deleted at expires_at
    (received_at + 48h). No email/phone/name is ever stored here.
    """
    __tablename__ = "pending_captures"
    __table_args__ = (
        Index("ix_pending_captures_tenant_order", "tenant_id", "platform_order_id"),
        Index("ix_pending_captures_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    platform_order_id: Mapped[str] = mapped_column(String(64))
    order_total: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))
    utm_source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    utm_medium: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    utm_campaign: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    utm_content: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ad_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    fbp: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    fbc: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ttclid: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    sccid: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ip_ciphertext: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ua_ciphertext: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    merged_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    rejected_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    def __repr__(self) -> str:
        return f"PendingCapture(id={self.id!r}, tenant_id={self.tenant_id!r})"  # no order id / ciphertext in reprs


class WebhookDelivery(Base):
    """
    Audit row per received webhook. `dedupe_key` is "<platform>:<delivery_id>" when the platform supplies a
    delivery id (Shopify X-Shopify-Webhook-Id), else "<platform>:<topic>:<payload_sha256>". It is a LOOKUP (not
    UNIQUE) so a delivery whose processing failed can be redelivered and reprocessed.
    """
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    platform: Mapped[str] = mapped_column(String(32))
    topic: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delivery_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), index=True)
    signature_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)  # None until S1-2
    processed_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    error_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payload_sha256: Mapped[str] = mapped_column(String(64))


class StagedWebhook(Base):
    """
    Durable copy of a verified webhook, written BEFORE the route answers 200 (webhook_staging.py). The payload is
    Fernet-encrypted (credentials.encrypt_value) and the row is deleted once process_webhook has run, so a background
    task lost between the 200 and the commit is replayed by the scheduler instead of being dropped. A row that keeps
    failing ends as status "dead" with its ciphertext wiped (no PII is kept for dead letters).
    """
    __tablename__ = "staged_webhooks"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'dead')", name="ck_staged_webhooks_status"),
        Index("ix_staged_webhooks_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32))
    topic: Mapped[str] = mapped_column(String(64))
    delivery_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Platform order reference (order id / courier business reference), not PII. Kept on dead letters so an operator
    # can reconcile the missing update with the merchant after the payload is wiped.
    order_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    payload_ciphertext: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    last_error_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:
        return f"StagedWebhook(id={self.id!r}, platform={self.platform!r}, status={self.status!r})"  # no ciphertext


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # no PII


class Digest(Base):
    __tablename__ = "digests"
    __table_args__ = (UniqueConstraint("tenant_id", "week_start", name="uq_digests_tenant_week"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    week_start: Mapped[date] = mapped_column(Date)
    sent_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    channel: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# --- Engine / session ------------------------------------------------------------------------------------

def make_engine(url: Optional[str] = None) -> Engine:
    """Builds an engine for `url` (default env DATABASE_URL, then a local SQLite file)."""
    url = url or os.environ.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool  # one shared connection, or each checkout would see an empty DB
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _enable_sqlite_fks(dbapi_conn, _record):  # SQLite ignores FKs unless asked
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def init_db(engine: Optional[Engine] = None) -> Engine:
    """Creates all tables (idempotent). Alembic replaces this later."""
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    return engine


@contextmanager
def session_scope(factory: Optional[sessionmaker] = None) -> Iterator[Session]:
    """Session that commits on success and rolls back on error."""
    session = (factory or get_session_factory())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- Tenant helpers --------------------------------------------------------------------------------------

def _norm_domain(shop_domain: str) -> str:
    return shop_domain.strip().lower()


def get_tenant_by_shop_domain(session: Session, shop_domain: str) -> Optional[Tenant]:
    """
    Looks a tenant up by `tenants.shop_domain`. Convention: for Shopify tenants this is the *.myshopify.com domain
    (matches the X-Shopify-Shop-Domain header); for Salla tenants it holds the Salla MERCHANT ID as a string (e.g.
    "1234567"), because Salla webhook payloads identify the store by the top-level `merchant` field, not a domain.
    """
    return session.scalar(select(Tenant).where(Tenant.shop_domain == _norm_domain(shop_domain)))


_TEST_EVENT_CODE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def normalize_test_event_code(code: Optional[str]) -> Optional[str]:
    """Trimmed Meta test_event_code, None for None/blank; raises ValueError for anything that is not a plain code."""
    cleaned = code.strip() if code else None
    if cleaned and not _TEST_EVENT_CODE.match(cleaned):
        raise ValueError("test_event_code must be 1-64 letters, digits, '_' or '-' (e.g. TEST12345)")
    return cleaned or None


def set_meta_test_event_code(session: Session, tenant: Tenant, code: Optional[str]) -> Optional[str]:
    """Sets (or with None/blank clears) the tenant's Meta test_event_code and commits. Returns the stored value."""
    tenant.meta_test_event_code = normalize_test_event_code(code)
    session.commit()
    return tenant.meta_test_event_code


def create_tenant(
    session: Session,
    name: str,
    platform: str,
    shop_domain: str,
    meta_dataset_id: Optional[str] = None,
    country: str = "EG",
    currency: str = "EGP",
    timezone: str = "Africa/Cairo",
    mode: str = "shadow",
    active: bool = True,
    settlement_hours: Optional[float] = None,
    storefront_url: Optional[str] = None,
    digest_email: Optional[str] = None,
    language: str = "ar"
) -> Tenant:
    """Creates and commits a tenant. New tenants default to shadow mode (no Meta calls) until flipped to live."""
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {PLATFORMS}")
    if mode not in TENANT_MODES:
        raise ValueError(f"mode must be one of {TENANT_MODES}")
    if settlement_hours is not None and settlement_hours < 0:
        raise ValueError("settlement_hours must be >= 0")
    if storefront_url is not None and not storefront_url.strip().lower().startswith(("http://", "https://")):
        raise ValueError("storefront_url must start with http:// or https://")
    if language not in ("ar", "en"):
        raise ValueError("language must be 'ar' or 'en'")
    tenant = Tenant(
        name=name, platform=platform, shop_domain=_norm_domain(shop_domain), meta_dataset_id=meta_dataset_id,
        country=country.upper(), currency=currency.upper(), timezone=timezone, mode=mode, active=active,
        settlement_hours=settlement_hours, storefront_url=storefront_url.strip() if storefront_url else None,
        digest_email=digest_email.strip() if digest_email and digest_email.strip() else None, language=language
    )
    session.add(tenant)
    session.commit()
    return tenant
