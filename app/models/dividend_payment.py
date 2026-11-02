import datetime
from sqlalchemy import Column, String, Float, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy import ForeignKeyConstraint, Index, text, CheckConstraint
from app.models.base import Base


class DividendPayment(Base):
    __tablename__ = "dividend_payments"
    __table_args__ = (
        UniqueConstraint("user_id", "platform_id", "source_namespace", "reference", name="uq_dividend_payments_account_source"),
        ForeignKeyConstraint(["platform_id", "user_id"], ["platforms.id", "platforms.user_id"], name="fk_dividend_payments_account_owner"),
        CheckConstraint("source_namespace = 'legacy' OR (user_id IS NOT NULL AND platform_id IS NOT NULL)", name="ck_dividend_payments_resolved_source_owner"),
        Index("uq_dividend_payments_unassigned_source", "user_id", "source_namespace", "reference", unique=True,
              postgresql_where=text("platform_id IS NULL")),
    )
    source_namespace = Column(String(80), nullable=False, default="legacy", server_default="legacy")

    id = Column(Integer, primary_key=True, autoincrement=True)
    reference = Column(String, nullable=False)   # T212 dividend reference
    ticker = Column(String, ForeignKey("instruments.ticker"), nullable=False)
    platform_id = Column(Integer, ForeignKey("platforms.id"), nullable=True, index=True)
    account = Column(String, nullable=True)  # deprecated: kept for backward compat, use platform_id
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    quantity = Column(Float)
    amount = Column(Float)              # actual GBP received
    gross_amount_per_share = Column(Float)
    paid_on = Column(DateTime)
    type = Column(String)               # DIVIDEND, DIVIDEND_MANUFACTURED_PAYMENT, etc.
    synced_at = Column(DateTime, default=datetime.datetime.utcnow)

    platform = relationship("Platform", back_populates="dividend_payments",
                            foreign_keys=[platform_id])
