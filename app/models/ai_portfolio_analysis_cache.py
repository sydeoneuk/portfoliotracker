import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.models.base import Base


class AIPortfolioAnalysisCache(Base):
    __tablename__ = "ai_portfolio_analysis_cache"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "account_filter",
            "pie_filter_key",
            "holdings_hash",
            name="uq_ai_portfolio_analysis_cache_scope",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String(50), nullable=False, default="anthropic")
    account_filter = Column(String(20), nullable=False)
    pie_filter_key = Column(String(255), nullable=False)
    holdings_hash = Column(String(64), nullable=False)
    model = Column(String(100), nullable=False)
    prompt_text = Column(Text, nullable=True)
    analysis_text = Column(Text, nullable=False)
    holdings_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )
