from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Legacy single-user T212 credentials (used by scripts/sync.py only)
    t212_api_key: str | None = None
    t212_api_secret: str | None = None
    t212_isa_api_key: str | None = None
    t212_isa_api_secret: str | None = None
    t212_base_url: str = "https://live.trading212.com/api/v0"

    # Optional: Financial Modeling Prep
    fmp_api_key: str | None = None

    postgres_db: str = "trading212"
    postgres_user: str = "trading212"
    postgres_password: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Web app security — generate with: python -c "import secrets; print(secrets.token_hex(32))"
    session_secret: str = "change-me-in-production"
    # Fernet key — generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = "change-me-in-production-must-be-fernet-key"

    # OAuth2 providers — configure at least one
    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None

    # Base URL for OAuth redirect URIs (no trailing slash)
    app_base_url: str = "http://localhost:8001"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
