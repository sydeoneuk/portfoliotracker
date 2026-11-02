import datetime
from sqlalchemy import Column, String, Float, DateTime, Integer, ForeignKey, UniqueConstraint
from sqlalchemy import ForeignKeyConstraint, Index, text, CheckConstraint
from app.models.base import Base


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("user_id", "platform_id", "source_namespace", "id", name="uq_transactions_account_source"),
        ForeignKeyConstraint(["platform_id", "user_id"], ["platforms.id", "platforms.user_id"], name="fk_transactions_account_owner"),
        CheckConstraint("source_namespace = 'legacy' OR (user_id IS NOT NULL AND platform_id IS NOT NULL)", name="ck_transactions_resolved_source_owner"),
        Index("uq_transactions_unassigned_source", "user_id", "source_namespace", "id", unique=True,
              postgresql_where=text("platform_id IS NULL")),
    )
    source_namespace = Column(String(80), nullable=False, default="legacy", server_default="legacy")

    pk = Column(Integer, primary_key=True, autoincrement=True)
    id = Column(String, nullable=False)   # T212 reference / transaction ID
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    platform_id = Column(Integer, ForeignKey("platforms.id"), nullable=True, index=True)
    type = Column(String(50))
    amount = Column(Float)
    date_time = Column(DateTime)
    reference = Column(String)
    notes = Column(String)

    synced_at = Column(DateTime, default=datetime.datetime.utcnow)
