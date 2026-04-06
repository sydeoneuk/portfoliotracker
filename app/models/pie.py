import datetime
from sqlalchemy import Column, String, Float, DateTime, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import Base


class Pie(Base):
    __tablename__ = "pies"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    account = Column(String(20))   # "ISA" or "Trading" — set at sync time
    icon = Column(String)
    goal = Column(Float)
    creation_date = Column(DateTime)
    end_date = Column(DateTime)
    initial_investment = Column(Float)
    dividend_cash_action = Column(String(20))

    last_synced_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    holdings = relationship("PieHolding", back_populates="pie", cascade="all, delete-orphan")


class PieHolding(Base):
    __tablename__ = "pie_holdings"
    __table_args__ = (UniqueConstraint("pie_id", "ticker", name="uq_pie_holdings_pie_ticker"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    pie_id = Column(Integer, ForeignKey("pies.id", ondelete="CASCADE"), nullable=False)
    ticker = Column(String, ForeignKey("instruments.ticker"), nullable=False)

    expected_share = Column(Float)
    current_share = Column(Float)
    owned_quantity = Column(Float)
    price_avg_invested_value = Column(Float)
    price_avg_value = Column(Float)
    price_avg_result = Column(Float)
    price_avg_result_coef = Column(Float)

    synced_at = Column(DateTime, default=datetime.datetime.utcnow)

    pie = relationship("Pie", back_populates="holdings")
    instrument = relationship("Instrument", back_populates="pie_holdings")
