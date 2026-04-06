import datetime
from sqlalchemy import Column, String, Float, DateTime, ForeignKey, Integer, UniqueConstraint
from app.models.base import Base


class DividendPayment(Base):
    __tablename__ = "dividend_payments"
    __table_args__ = (UniqueConstraint("user_id", "reference", name="uq_dividend_payments_user_ref"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    reference = Column(String, nullable=False)   # T212 dividend reference
    ticker = Column(String, ForeignKey("instruments.ticker"), nullable=False)
    account = Column(String, nullable=False)  # "ISA" or "Trading"
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    quantity = Column(Float)
    amount = Column(Float)              # actual GBP received
    gross_amount_per_share = Column(Float)
    paid_on = Column(DateTime)
    type = Column(String)               # DIVIDEND, DIVIDEND_MANUFACTURED_PAYMENT, etc.
    synced_at = Column(DateTime, default=datetime.datetime.utcnow)
