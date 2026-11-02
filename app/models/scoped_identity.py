import datetime
from sqlalchemy import Column, Integer, String, ForeignKey, ForeignKeyConstraint, UniqueConstraint, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
from app.models.base import Base


class AccountListingResolution(Base):
    __tablename__ = 'account_listing_resolutions'
    __table_args__ = (
        UniqueConstraint('platform_id', 'source_namespace', 'ticker', name='uq_account_listing_scope'),
        ForeignKeyConstraint(['platform_id', 'user_id'], ['platforms.id', 'platforms.user_id']),
        ForeignKeyConstraint(['provider_mapping_id', 'listing_id'], ['provider_listing_mappings.id', 'provider_listing_mappings.listing_id']),
        Index('ix_account_listing_resolution_owner', 'user_id', 'platform_id'),
    )
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    platform_id = Column(Integer, ForeignKey('platforms.id'), nullable=False)
    source_namespace = Column(String(120), nullable=False)
    ticker = Column(String, ForeignKey('instruments.ticker'), nullable=False)
    listing_id = Column(Integer, ForeignKey('listings.id'), nullable=False)
    provider_mapping_id = Column(Integer, ForeignKey('provider_listing_mappings.id'), nullable=False)
    source_fingerprint = Column(String(64), nullable=False)
    claim = Column(JSONB, nullable=False)
    evidence = Column(JSONB, nullable=False)
    revision = Column(Integer, nullable=False)
    verified_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)


class AccountListingAudit(Base):
    __tablename__ = 'account_listing_audits'
    __table_args__ = (
        ForeignKeyConstraint(['platform_id', 'user_id'], ['platforms.id', 'platforms.user_id']),
        Index('ix_account_listing_audit_scope', 'platform_id', 'source_namespace', 'ticker', 'id'),
    )
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)  # owner
    actor_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'))
    platform_id = Column(Integer, ForeignKey('platforms.id'), nullable=False)
    source_namespace = Column(String(120), nullable=False)
    ticker = Column(String, nullable=False)
    action = Column(String(20), nullable=False)
    review_key = Column(String(36), unique=True)
    before = Column(JSONB)
    after = Column(JSONB)
    undo_of = Column(Integer, ForeignKey('account_listing_audits.id'), unique=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
