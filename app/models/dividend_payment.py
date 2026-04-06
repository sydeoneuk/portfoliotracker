import datetime
from sqlalchemy import Column, String, Float, DateTime, ForeignKey, Integer
from app.models.base import Base


class DividendPayment(Base):
    __tablename__ = "dividend_payments"

    reference = Column(String, primary_key=True)
    ticker = Column(String, ForeignKey("instruments.ticker"), nullable=False)
    account = Column(String, nullable=False)  # "ISA" or "Trading"
    user_id = Column(Integer, ForeignKey("users.id"))
    quantity = Column(Float)
    amount = Column(Float)              # actual GBP received
    gross_amount_per_share = Column(Float)
    paid_on = Column(DateTime)
    type = Column(String)               # DIVIDEND, DIVIDEND_MANUFACTURED_PAYMENT, etc.
    synced_at = Column(DateTime, default=datetime.datetime.utcnow)
