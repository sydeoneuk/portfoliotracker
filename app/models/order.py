import datetime
from sqlalchemy import Column, String, Float, DateTime, Integer, ForeignKey, UniqueConstraint, Numeric, Date
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy import ForeignKeyConstraint, Index, text, CheckConstraint
from app.models.base import Base


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("user_id", "platform_id", "source_namespace", "id", name="uq_orders_account_source"),
        ForeignKeyConstraint(["platform_id", "user_id"], ["platforms.id", "platforms.user_id"], name="fk_orders_account_owner"),
        CheckConstraint("source_namespace = 'legacy' OR (user_id IS NOT NULL AND platform_id IS NOT NULL)", name="ck_orders_resolved_source_owner"),
        Index("uq_orders_unassigned_source", "user_id", "source_namespace", "id", unique=True,
              postgresql_where=text("platform_id IS NULL")),
    )
    source_namespace = Column(String(80), nullable=False, default="legacy", server_default="legacy")

    pk = Column(Integer, primary_key=True, autoincrement=True)
    id = Column(String, nullable=False)   # T212 order ID
    ticker = Column(String, ForeignKey("instruments.ticker"))
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    platform_id = Column(Integer, ForeignKey("platforms.id"), nullable=True, index=True)
    account = Column(String(20))  # deprecated: kept for backward compat, use platform_id
    quantity = Column(Float)
    filled_quantity = Column(Float)
    order_type = Column(String(20))
    side = Column(String(10))    # BUY | SELL
    status = Column(String(20))
    limit_price = Column(Float)
    stop_price = Column(Float)
    fill_price = Column(Float)
    # Legacy fill_price is retained. New fields have explicit units.
    execution_price = Column(Numeric(28, 10))
    execution_currency = Column(String(10))
    settlement_amount = Column(Numeric(28, 10))
    settlement_currency = Column(String(10))
    settlement_date = Column(Date)
    execution_fx_rate = Column(Numeric(28, 12))
    charges = Column(JSONB)
    time_validity = Column(String(10))
    created_at = Column(DateTime)
    filled_at = Column(DateTime)

    synced_at = Column(DateTime, default=datetime.datetime.utcnow)

    instrument = relationship("Instrument", back_populates="orders")
    platform = relationship("Platform", back_populates="orders",
                            foreign_keys=[platform_id])


class TradeImportObservation(Base):
    __tablename__ = "trade_import_observations"
    __table_args__ = (UniqueConstraint("order_pk", "payload_hash", name="uq_trade_observation_payload"),)
    id = Column(Integer, primary_key=True)
    order_pk = Column(Integer, ForeignKey("orders.pk", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    payload_hash = Column(String(64), nullable=False)
    payload = Column(JSONB, nullable=False)
    imported_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
