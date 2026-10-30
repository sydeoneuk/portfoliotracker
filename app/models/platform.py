import datetime
from sqlalchemy import Boolean, Column, DateTime, Integer, ForeignKey, String
from sqlalchemy.orm import relationship
from app.models.base import Base

# Valid platform_type values
PLATFORM_TYPE_T212_API = "t212_api"
PLATFORM_TYPE_T212_CSV = "t212_csv"
PLATFORM_TYPE_INTERACTIVE_INVESTOR = "interactive_investor"
PLATFORM_TYPE_CHARLES_STANLEY = "charles_stanley"
PLATFORM_TYPE_BEST_INVEST = "best_invest"

# Valid account_type values
ACCOUNT_TYPE_GIA = "GIA"       # General Investment Account
ACCOUNT_TYPE_ISA = "ISA"       # Stocks & Shares ISA
ACCOUNT_TYPE_SIPP = "SIPP"     # Self-Invested Personal Pension
ACCOUNT_TYPE_LISA = "LISA"     # Lifetime ISA


class Platform(Base):
    __tablename__ = "platforms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)          # e.g. "Trading 212 ISA"
    platform_type = Column(String(50), nullable=False)  # t212_api, t212_csv, …
    account_type = Column(String(20), nullable=False)   # ISA, SIPP, GIA, LISA
    currency = Column(String(10), default="GBP", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)

    positions = relationship("Position", back_populates="platform",
                             foreign_keys="Position.platform_id")
    orders = relationship("Order", back_populates="platform",
                          foreign_keys="Order.platform_id")
    dividend_payments = relationship("DividendPayment", back_populates="platform",
                                     foreign_keys="DividendPayment.platform_id")
    pies = relationship("Pie", back_populates="platform",
                        foreign_keys="Pie.platform_id")
