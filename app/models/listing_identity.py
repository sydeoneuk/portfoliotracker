"""Stable catalogue identities alongside retained provider-ticker keys."""
import datetime
from sqlalchemy import Column, Integer, String, ForeignKey, ForeignKeyConstraint, DateTime, Numeric, UniqueConstraint, CheckConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB
from app.models.base import Base


class Security(Base):
    __tablename__ = 'securities'
    id = Column(Integer, primary_key=True)
    isin = Column(String(12), nullable=False, unique=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)


class Listing(Base):
    __tablename__ = 'listings'
    __table_args__ = (UniqueConstraint('security_id', 'mic', 'quote_currency', 'unit_scale', 'venue_symbol', name='uq_listing_identity'),
                      CheckConstraint('unit_scale > 0', name='ck_listing_unit_scale'))
    id = Column(Integer, primary_key=True)
    security_id = Column(Integer, ForeignKey('securities.id'), nullable=False)
    mic = Column(String(4), nullable=False)
    quote_currency = Column(String(3), nullable=False)  # monetary currency, e.g. GBP
    unit_scale = Column(Numeric(12, 8), nullable=False)  # GBX quote = GBP × .01
    venue_symbol = Column(String(40), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)


class ProviderListingMapping(Base):
    __tablename__ = 'provider_listing_mappings'
    __table_args__ = (UniqueConstraint('provider', 'namespace', 'identifier', name='uq_provider_listing_identifier'), UniqueConstraint('id', 'listing_id', name='uq_provider_mapping_listing'))
    id = Column(Integer, primary_key=True)
    provider = Column(String(40), nullable=False)
    namespace = Column(String(40), nullable=False)
    identifier = Column(String(80), nullable=False)
    listing_id = Column(Integer, ForeignKey('listings.id'), nullable=False)
    evidence = Column(JSONB, nullable=False)
    verified_at = Column(DateTime, nullable=False)


class InstrumentListingLink(Base):
    __tablename__ = 'instrument_listing_links'
    __table_args__ = (ForeignKeyConstraint(['provider_mapping_id', 'listing_id'], ['provider_listing_mappings.id', 'provider_listing_mappings.listing_id']),
                      Index('ix_instrument_listing_links_listing', 'listing_id'))
    ticker = Column(String, ForeignKey('instruments.ticker'), primary_key=True)
    listing_id = Column(Integer, ForeignKey('listings.id'), nullable=False)
    provider_mapping_id = Column(Integer, ForeignKey('provider_listing_mappings.id'), nullable=False)
    source_fingerprint = Column(String(64), nullable=False)
    revision = Column(Integer, nullable=False)
    evidence = Column(JSONB, nullable=False)
    verified_at = Column(DateTime, nullable=False)


class ListingIdentityAudit(Base):
    __tablename__ = 'listing_identity_audits'
    __table_args__ = (Index('ix_listing_identity_audits_ticker', 'ticker', 'id'),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'))  # administering actor, not ownership
    ticker = Column(String, nullable=False)
    action = Column(String(20), nullable=False)
    before = Column(JSONB)
    after = Column(JSONB)
    undo_of = Column(Integer, ForeignKey('listing_identity_audits.id'), unique=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
