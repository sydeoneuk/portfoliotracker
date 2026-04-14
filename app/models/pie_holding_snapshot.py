import datetime
from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from app.models.base import Base


class PieHoldingSnapshot(Base):
    __tablename__ = "pie_holding_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "pie_id",
            "ticker",
            "snapshot_date",
            name="uq_pie_holding_snapshots_daily_ticker",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    pie_id = Column(Integer, ForeignKey("pies.pk", ondelete="CASCADE"), nullable=False, index=True)
    ticker = Column(String, ForeignKey("instruments.ticker"), nullable=False, index=True)
    account = Column(String(20), nullable=True, index=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    captured_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    owned_quantity = Column(Float, nullable=False, default=0.0)
    current_share = Column(Float)
    expected_share = Column(Float)
