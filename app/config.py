"""Runtime configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service settings.

    ``GEMINI_API_KEY``, ``REDIS_URL``, ``CHROMA_PERSIST_DIR`` and
    ``CACHE_TTL_SECONDS`` are the operational knobs. Model identifiers have
    defaults that match the service contract and can be overridden without
    a code change.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    gemini_api_key: str = ""
    redis_url: str = "redis://localhost:6379/0"
    chroma_persist_dir: str = "./chroma_db"
    cache_ttl_seconds: int = Field(default=3600, ge=1)

    gemini_model: str = "gemini-2.5-flash"
    gemini_model_fallback: str = "gemini-3.8-flash"
    embedding_model: str = "text-embedding-004"
    embedding_model_fallback: str = "gemini-embedding-001"
    gemini_timeout_ms: int = Field(default=90_000, ge=1_000)

    max_automatic_refund_brl: float = Field(default=150.0, gt=0)
    rag_top_k: int = Field(default=2, ge=1, le=10)

    @field_validator(
        "gemini_api_key",
        "redis_url",
        "chroma_persist_dir",
        "gemini_model",
        "gemini_model_fallback",
        "embedding_model",
        "embedding_model_fallback",
        mode="before",
    )
    @classmethod
    def _strip_strings(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""

    return Settings()
