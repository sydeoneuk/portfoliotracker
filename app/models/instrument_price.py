import datetime
from sqlalchemy import Column, String, Numeric, DateTime, ForeignKey
from app.models.base import Base


class InstrumentPrice(Base):
    __tablename__ = "instrument_prices"

    ticker     = Column(String, ForeignKey("instruments.ticker", ondelete="CASCADE"), primary_key=True)
    source     = Column(String(50), primary_key=True)  # 't212_api' | 'yfinance' | 'manual'
    price      = Column(Numeric(18, 6), nullable=False)   # native currency
    price_gbp  = Column(Numeric(18, 6), nullable=True)    # GBP equivalent
    currency   = Column(String(10), nullable=False, default="GBP")
    updated_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
