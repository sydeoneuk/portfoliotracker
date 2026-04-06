import datetime
from sqlalchemy import Column, String, Float, DateTime, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import Base


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("user_id", "id", name="uq_orders_user_t212"),)

    pk = Column(Integer, primary_key=True, autoincrement=True)
    id = Column(String, nullable=False)   # T212 order ID
    ticker = Column(String, ForeignKey("instruments.ticker"))
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    account = Column(String(20))
    quantity = Column(Float)
    filled_quantity = Column(Float)
    order_type = Column(String(20))
    side = Column(String(10))    # BUY | SELL
    status = Column(String(20))
    limit_price = Column(Float)
    stop_price = Column(Float)
    fill_price = Column(Float)
    time_validity = Column(String(10))
    created_at = Column(DateTime)
    filled_at = Column(DateTime)

    synced_at = Column(DateTime, default=datetime.datetime.utcnow)

    instrument = relationship("Instrument", back_populates="orders")
