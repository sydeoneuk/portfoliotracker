import datetime
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from app.models.base import Base


class UserInstrumentMapping(Base):
    __tablename__ = "user_instrument_mappings"
    __table_args__ = (UniqueConstraint("user_id", "ticker", name="uq_user_mapping_ticker"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ticker = Column(String, ForeignKey("instruments.ticker"), nullable=False)
    isin = Column(String(20), nullable=False)
    listing_currency = Column(String(10), nullable=False)
    provider_symbol = Column(String(80), nullable=False)
    quote_currency = Column(String(10), nullable=False)
    exchange = Column(String(100))
    evidence = Column(JSONB, nullable=False)
    price = Column(Numeric(28, 10))
    normalized_price = Column(Numeric(28, 10))
    conversion_rate = Column(Numeric(28, 12))
    price_observed_at = Column(DateTime)
    checked_at = Column(DateTime, nullable=False)
    revision = Column(Integer, nullable=False)


class InstrumentMappingProposal(Base):
    __tablename__ = "instrument_mapping_proposals"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ticker = Column(String, ForeignKey("instruments.ticker"), nullable=False)
    status = Column(String(30), nullable=False)
    baseline = Column(JSONB, nullable=False)
    candidate = Column(JSONB, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)


class InstrumentMappingAudit(Base):
    __tablename__ = "instrument_mapping_audits"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ticker = Column(String, ForeignKey("instruments.ticker"), nullable=False)
    proposal_id = Column(Integer, ForeignKey("instrument_mapping_proposals.id"))
    action = Column(String(20), nullable=False)
    before = Column(JSONB)
    after = Column(JSONB)
    undo_of = Column(Integer, ForeignKey("instrument_mapping_audits.id"), unique=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
