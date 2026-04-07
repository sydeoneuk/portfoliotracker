import datetime
from sqlalchemy import Boolean, Column, Integer, String, DateTime, ForeignKey, Text, Float
from sqlalchemy.orm import relationship
from app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255))
    provider = Column(String(50), nullable=False)   # 'google' | 'microsoft'
    provider_id = Column(String(255), nullable=False)
    avatar_url = Column(String(500))
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_login_at = Column(DateTime)

    settings = relationship("UserSettings", back_populates="user", uselist=False,
                            cascade="all, delete-orphan")


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    t212_api_key_enc = Column(Text)        # encrypted Trading account API key
    t212_api_secret_enc = Column(Text)     # encrypted Trading account API secret
    t212_isa_api_key_enc = Column(Text)    # encrypted ISA account API key (optional)
    t212_isa_api_secret_enc = Column(Text) # encrypted ISA account API secret (optional)
    last_sync_at = Column(DateTime)
    sync_status = Column(String(20), default="idle")  # idle | running | done | error
    sync_message = Column(Text)
    free_cash_trading = Column(Float)   # uninvested GBP cash in Trading account (outside pies)
    free_cash_isa = Column(Float)       # uninvested GBP cash in ISA account (outside pies)
    pie_cash_trading = Column(Float)    # uninvested cash sitting inside pies, Trading account
    pie_cash_isa = Column(Float)        # uninvested cash sitting inside pies, ISA account
    auto_sync_enabled = Column(Boolean, default=True, nullable=False, server_default="true")

    user = relationship("User", back_populates="settings")
