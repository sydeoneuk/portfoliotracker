import datetime
from sqlalchemy import Column, String, Float, DateTime, ForeignKey, Integer, Numeric, Text
from sqlalchemy.orm import relationship
from app.models.base import Base


class Position(Base):
    __tablename__ = "positions"

    ticker = Column(String, ForeignKey("instruments.ticker"), primary_key=True)
    platform_id = Column(Integer, ForeignKey("platforms.id"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    account = Column(String, nullable=True)  # deprecated: kept for backward compat, use platform_id
    quantity = Column(Float)
    average_price = Column(Float)
    cost_basis_amount = Column(Numeric(28, 10))
    cost_basis_currency = Column(String(10))
    cost_basis_status = Column(String(30))  # reported | estimated | unknown
    cost_basis_source = Column(String(30))  # broker | transactions | pie
    cost_basis_reason = Column(Text)
    current_price = Column(Float)
    ppl = Column(Float)
    fx_ppl = Column(Float)
    result_coef = Column(Float)

    last_synced_at = Column(DateTime, default=datetime.datetime.utcnow)

    instrument = relationship("Instrument", back_populates="positions")
    platform = relationship("Platform", back_populates="positions",
                            foreign_keys=[platform_id])
