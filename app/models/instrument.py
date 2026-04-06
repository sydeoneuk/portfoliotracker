import datetime
from sqlalchemy import Column, String, Float, DateTime
from sqlalchemy.orm import relationship
from app.models.base import Base


class Instrument(Base):
    __tablename__ = "instruments"

    ticker = Column(String, primary_key=True)
    name = Column(String)
    short_name = Column(String)
    currency_code = Column(String(10))
    isin = Column(String(20))
    instrument_type = Column(String(50))
    exchange = Column(String(100))
    min_trade_quantity = Column(Float)
    max_open_quantity = Column(Float)

    # Enriched via third-party APIs
    sector = Column(String(100))
    industry = Column(String(100))
    market_cap = Column(Float)
    description = Column(String)
    country = Column(String(100))
    last_enriched_at = Column(DateTime)

    # Yahoo Finance ticker mapping (auto-derived or manually set)
    # e.g. T212 "VWRPl_EQ" → yf_ticker "VWRP.L"
    yf_ticker = Column(String(30))
    last_dividend_synced_at = Column(DateTime)
    fcf_per_share_3y_avg = Column(Float)
    eps_ttm = Column(Float)
    instrument_class = Column(String(50))

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    pie_holdings = relationship("PieHolding", back_populates="instrument")
    positions = relationship("Position", back_populates="instrument")
    orders = relationship("Order", back_populates="instrument")
    dividend_history = relationship("DividendHistory", back_populates="instrument")
    dividend_forecast = relationship("DividendForecast", back_populates="instrument")
