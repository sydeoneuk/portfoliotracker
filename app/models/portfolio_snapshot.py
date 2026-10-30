import datetime
from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from app.models.base import Base


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "snapshot_date",
            "platform_id",
            "scope_type",
            "pie_id",
            name="uq_portfolio_snapshots_daily_scope",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    captured_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    platform_id = Column(Integer, ForeignKey("platforms.id"), nullable=True, index=True)
    account = Column(String(20), nullable=True, index=True)  # deprecated: use platform_id
    scope_type = Column(String(20), nullable=False)  # account | pie
    pie_id = Column(Integer, ForeignKey("pies.pk", ondelete="CASCADE"), nullable=True, index=True)
    total_value_gbp = Column(Float, nullable=False, default=0.0)
