import datetime
from sqlalchemy import Column, String, Float, DateTime
from app.models.base import Base


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True)
    type = Column(String(50))
    amount = Column(Float)
    date_time = Column(DateTime)
    reference = Column(String)
    notes = Column(String)

    synced_at = Column(DateTime, default=datetime.datetime.utcnow)
