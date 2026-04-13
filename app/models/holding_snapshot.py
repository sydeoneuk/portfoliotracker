import datetime
from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from app.models.base import Base


class HoldingSnapshot(Base):
    __tablename__ = "holding_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "snapshot_date",
            "account",
            "ticker",
            name="uq_holding_snapshots_daily_ticker",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    captured_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    account = Column(String(20), nullable=False, index=True)
    ticker = Column(String, ForeignKey("instruments.ticker"), nullable=False, index=True)
    quantity = Column(Float, nullable=False, default=0.0)
    price_native = Column(Float, nullable=False, default=0.0)
    value_gbp = Column(Float, nullable=False, default=0.0)
