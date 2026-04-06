import datetime
from sqlalchemy import Column, String, Float, DateTime, ForeignKey, Integer
from sqlalchemy.orm import relationship
from app.models.base import Base


class Position(Base):
    __tablename__ = "positions"

    ticker = Column(String, ForeignKey("instruments.ticker"), primary_key=True)
    account = Column(String, primary_key=True)  # "ISA" or "Trading"
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    quantity = Column(Float)
    average_price = Column(Float)
    current_price = Column(Float)
    ppl = Column(Float)
    fx_ppl = Column(Float)
    result_coef = Column(Float)

    last_synced_at = Column(DateTime, default=datetime.datetime.utcnow)

    instrument = relationship("Instrument", back_populates="positions")
