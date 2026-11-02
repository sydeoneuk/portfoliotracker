"""Retained source uploads and observations; current state is a projection."""
import datetime
from sqlalchemy import Column, Integer, String, Date, DateTime, Boolean, LargeBinary, ForeignKey, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from app.models.base import Base


class IngestionBatch(Base):
    __tablename__ = 'ingestion_batches'
    __table_args__ = (ForeignKeyConstraint(['platform_id', 'user_id'], ['platforms.id', 'platforms.user_id']), UniqueConstraint('id', 'user_id'))
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    platform_id = Column(Integer, ForeignKey('platforms.id'), nullable=False, index=True)
    source = Column(String(40), nullable=False)
    filename = Column(String(255), nullable=False)
    checksum = Column(String(64), nullable=False, index=True)
    parser_version = Column(String(30), nullable=False)
    raw_content = Column(LargeBinary, nullable=False)
    effective_date = Column(Date)
    history_complete = Column(Boolean, nullable=False, default=False)
    options = Column(JSONB, nullable=False)
    status = Column(String(30), nullable=False)
    baseline = Column(String(64))
    review = Column(JSONB, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    applied_at = Column(DateTime)


class IngestionRow(Base):
    __tablename__ = 'ingestion_rows'
    __table_args__ = (UniqueConstraint('batch_id', 'row_number'), ForeignKeyConstraint(['batch_id', 'user_id'], ['ingestion_batches.id', 'ingestion_batches.user_id']))
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey('ingestion_batches.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    row_number = Column(Integer, nullable=False)
    cells = Column(JSONB, nullable=False)  # ordered cells retain duplicate headers


class ImportHoldingObservation(Base):
    __tablename__ = 'import_holding_observations'
    __table_args__ = (ForeignKeyConstraint(['batch_id', 'user_id'], ['ingestion_batches.id', 'ingestion_batches.user_id']),)
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey('ingestion_batches.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    phase = Column(String(20), nullable=False)  # before / proposed
    scope = Column(String(180), nullable=False)
    effective_date = Column(Date)
    complete = Column(Boolean, nullable=False)
    holdings = Column(JSONB, nullable=False)


class ImportAuthority(Base):
    __tablename__ = 'import_authorities'
    __table_args__ = (ForeignKeyConstraint(['platform_id', 'user_id'], ['platforms.id', 'platforms.user_id']),
                      ForeignKeyConstraint(['batch_id', 'user_id'], ['ingestion_batches.id', 'ingestion_batches.user_id']))
    platform_id = Column(Integer, ForeignKey('platforms.id'), primary_key=True)
    scope = Column(String(180), primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    batch_id = Column(Integer, ForeignKey('ingestion_batches.id'), nullable=False)
    effective_date = Column(Date, nullable=False)
