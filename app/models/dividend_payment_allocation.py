import datetime
from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from app.models.base import Base


class DividendPaymentAllocation(Base):
    __tablename__ = "dividend_payment_allocations"
    __table_args__ = (
        UniqueConstraint(
            "dividend_payment_id",
            "pie_id",
            name="uq_dividend_payment_allocations_payment_pie",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    dividend_payment_id = Column(
        Integer,
        ForeignKey("dividend_payments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    pie_id = Column(Integer, ForeignKey("pies.pk", ondelete="CASCADE"), nullable=False, index=True)
    ticker = Column(String, ForeignKey("instruments.ticker"), nullable=False, index=True)
    account = Column(String(20), nullable=False, index=True)
    amount_gbp = Column(Float, nullable=False, default=0.0)
    quantity = Column(Float, nullable=False, default=0.0)
    allocation_ratio = Column(Float, nullable=False, default=0.0)
    basis_snapshot_date = Column(Date, nullable=True, index=True)
    synced_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
