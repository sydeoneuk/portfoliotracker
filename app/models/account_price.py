"""Derived quotes for verified account holdings; financial records stay immutable."""
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Numeric
from sqlalchemy.dialects.postgresql import JSONB
from app.models.base import Base


class AccountListingPrice(Base):
    __tablename__ = 'account_listing_prices'
    resolution_id = Column(Integer, ForeignKey('account_listing_resolutions.id', ondelete='CASCADE'), primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    resolution_revision = Column(Integer, nullable=False)
    source_fingerprint = Column(String(64), nullable=False)
    authority_batch_id = Column(Integer, ForeignKey('ingestion_batches.id', ondelete='CASCADE'))
    catalogue_metadata = Column(JSONB, nullable=False)
    raw_price = Column(Numeric(28, 10))
    quote_currency = Column(String(10))
    unit_scale = Column(Numeric(12, 8))
    normalized_price = Column(Numeric(28, 10))
    normalized_currency = Column(String(10))
    conversion_rate = Column(Numeric(28, 14))
    observed_at = Column(DateTime)
    fetched_at = Column(DateTime)
    expires_at = Column(DateTime)
    checked_at = Column(DateTime, nullable=False)
    error = Column(String(500))

