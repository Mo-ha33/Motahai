"""
db.py — Persistence layer (SQLAlchemy 2.0) for the Motahai conversion pipeline (S1-1).

Works on SQLite (dev/tests) and Postgres (prod): only portable column types and constraints are used.
Engine URL comes from env DATABASE_URL (default sqlite:///./motahai.db), read lazily.

Privacy: customer email/phone are stored ONLY as SHA-256 hashes (orders.email_hash / phone_hash) and
webhook payloads are never stored (only their SHA-256). Credentials live encrypted (see credentials.py).

All timestamps are timezone-aware UTC. SQLite drops tzinfo on read, so UTCDateTime restores it.

Schema is created with create_all() for now.
TODO(follow-up): move to Alembic migrations before the first production schema change.
"""

import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text,
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
CAPI_STATUSES = ("pending", "sent", "failed", "shadow", "stale")
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
        CheckConstraint("status IN ('pending', 'sent', 'failed', 'shadow', 'stale')", name="ck_capi_events_status"),
        Index("ix_capi_events_status_created", "status", "created_at"),  # retry sweep
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
    active: bool = True
) -> Tenant:
    """Creates and commits a tenant. New tenants default to shadow mode (no Meta calls) until flipped to live."""
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {PLATFORMS}")
    if mode not in TENANT_MODES:
        raise ValueError(f"mode must be one of {TENANT_MODES}")
    tenant = Tenant(
        name=name, platform=platform, shop_domain=_norm_domain(shop_domain), meta_dataset_id=meta_dataset_id,
        country=country.upper(), currency=currency.upper(), timezone=timezone, mode=mode, active=active
    )
    session.add(tenant)
    session.commit()
    return tenant
