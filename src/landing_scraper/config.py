"""Centralized configuration via env vars."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "rajailayaperumal"
    postgres_password: str = ""
    postgres_db: str = "ipeds"

    anthropic_api_key: str = ""
    openai_api_key: str = ""
    tavily_api_key: str = ""
    google_api_key: str = ""
    google_cse_id: str = ""
    brandfetch_api_key: str = ""
    higheredjobs_api_key: str = ""

    llm_daily_cost_warn_usd: float = 10.0

    raw_payload_dir: Path = Path("data/raw_payloads")

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def has_llm(self) -> bool:
        return self.has_anthropic or self.has_openai

    @property
    def has_tavily(self) -> bool:
        return bool(self.tavily_api_key)

    @property
    def has_google_cse(self) -> bool:
        return bool(self.google_api_key and self.google_cse_id)

    @property
    def has_brandfetch(self) -> bool:
        return bool(self.brandfetch_api_key)


settings = Settings()
