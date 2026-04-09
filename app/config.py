from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Legacy single-user T212 credentials (used by scripts/sync.py only)
    t212_api_key: str | None = None
    t212_api_secret: str | None = None
    t212_isa_api_key: str | None = None
    t212_isa_api_secret: str | None = None
    t212_base_url: str = "https://live.trading212.com/api/v0"

    # Optional: Financial Modeling Prep
    fmp_api_key: str | None = None

    # Optional: OpenFIGI — used for FIGI/MIC/security-type enrichment
    # Without a key: 25 requests per 10 s (100 ISINs each → 250 instruments/s)
    # Free key at https://www.openfigi.com/api — raises limit to 25 req/s
    openfigi_api_key: str | None = None

    # Optional: Anthropic Claude — used as fallback description enricher
    anthropic_api_key: str | None = None

    postgres_db: str = "trading212"
    postgres_user: str = "trading212"
    postgres_password: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Web app security — generate with: python -c "import secrets; print(secrets.token_hex(32))"
    session_secret: str = "change-me-in-production"
    # Fernet key — generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = "change-me-in-production-must-be-fernet-key"

    def model_post_init(self, __context) -> None:
        """Refuse to start with default secret values."""
        insecure_defaults = {
            "change-me-in-production",
            "change-me-in-production-must-be-fernet-key",
        }
        if self.session_secret in insecure_defaults:
            raise ValueError(
                "SESSION_SECRET is set to an insecure default. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        if self.encryption_key in insecure_defaults:
            raise ValueError(
                "ENCRYPTION_KEY is set to an insecure default. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )

    # OAuth2 providers — configure at least one
    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None

    # Base URL for OAuth redirect URIs (no trailing slash)
    app_base_url: str = "http://localhost:8001"

    # Set to True when running behind HTTPS in production
    https_only: bool = False

    # Comma-separated list of email addresses granted admin access
    # e.g. ADMIN_EMAILS=paul@sydeone.co.uk,other@example.com
    admin_emails: str = ""

    @property
    def admin_email_set(self) -> set[str]:
        """Return the admin emails as a normalised lowercase set."""
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
