"""Fernet symmetric encryption for storing API keys at rest."""
from cryptography.fernet import Fernet, InvalidToken
from app.config import settings


def _fernet() -> Fernet:
    key = settings.encryption_key
    # Accept raw key string — must be a valid URL-safe base64-encoded 32-byte key
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, Exception):
        return None
