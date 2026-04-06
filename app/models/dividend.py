import datetime
from sqlalchemy import Column, String, Float, DateTime, Date, Integer, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import Base


class DividendHistory(Base):
    """Actual historical dividend payments for held instruments."""
    __tablename__ = "dividend_history"
    __table_args__ = (
        UniqueConstraint("ticker", "ex_date", name="uq_dividend_history_ticker_exdate"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, ForeignKey("instruments.ticker"), nullable=False, index=True)
    ex_date = Column(Date, nullable=False)
    pay_date = Column(Date)          # available from FMP; NULL when only yfinance data
    record_date = Column(Date)       # available from FMP
    declaration_date = Column(Date)  # available from FMP
    amount = Column(Float, nullable=False)   # per share, in instrument's currency
    adj_amount = Column(Float)       # split-adjusted amount (FMP)
    currency = Column(String(10))
    source = Column(String(50))      # 'yfinance' | 'fmp'
    fetched_at = Column(DateTime, default=datetime.datetime.utcnow)

    instrument = relationship("Instrument", back_populates="dividend_history")


class DividendForecast(Base):
    """Upcoming confirmed and projected future dividend payments."""
    __tablename__ = "dividend_forecast"
    __table_args__ = (
        UniqueConstraint("ticker", "ex_date", name="uq_dividend_forecast_ticker_exdate"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, ForeignKey("instruments.ticker"), nullable=False, index=True)
    ex_date = Column(Date)
    pay_date = Column(Date)
    amount = Column(Float)           # per share (last known or estimated)
    is_estimated = Column(Boolean, default=True)   # False = confirmed date from API
    frequency = Column(String(20))   # MONTHLY | QUARTERLY | SEMI_ANNUAL | ANNUAL | IRREGULAR
    annual_rate = Column(Float)      # annualised dividend rate per share
    dividend_yield = Column(Float)   # as a decimal, e.g. 0.032 = 3.2%
    source = Column(String(50))
    fetched_at = Column(DateTime, default=datetime.datetime.utcnow)

    instrument = relationship("Instrument", back_populates="dividend_forecast")
