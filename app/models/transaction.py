import datetime
from sqlalchemy import Column, String, Float, DateTime, Integer, ForeignKey, UniqueConstraint
from app.models.base import Base


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("user_id", "id", name="uq_transactions_user_t212"),)

    pk = Column(Integer, primary_key=True, autoincrement=True)
    id = Column(String, nullable=False)   # T212 reference / transaction ID
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    type = Column(String(50))
    amount = Column(Float)
    date_time = Column(DateTime)
    reference = Column(String)
    notes = Column(String)

    synced_at = Column(DateTime, default=datetime.datetime.utcnow)
