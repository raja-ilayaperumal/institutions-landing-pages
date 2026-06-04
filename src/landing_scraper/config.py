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
    # Additional Tavily keys for capacity scaling — round-robin across all
    # populated keys; fall through to the next key on per-key quota errors.
    # Add TAVILY_API_KEY_1, TAVILY_API_KEY_2, ... in .env as needed.
    tavily_api_key_1: str = ""
    tavily_api_key_2: str = ""
    tavily_api_key_3: str = ""
    google_api_key: str = ""
    google_cse_id: str = ""
    # Defense against the 2026-05-27 incident (~9.7k CSE queries in one
    # day → ₹4k). Even with key+CX present, CSE is skipped unless this
    # flag is explicitly set. Re-enable only AFTER configuring a daily
    # quota cap in GCP Console → IAM/Quotas → customsearch.googleapis.com.
    enable_google_cse: bool = False
    serper_api_key: str = ""
    # Additional Serper keys for capacity scaling — round-robin across all
    # populated keys; fall through to the next key on 429 / credit-exhausted.
    # Add SERPER_API_KEY_1, SERPER_API_KEY_2, ... in .env as needed.
    serper_api_key_1: str = ""
    serper_api_key_2: str = ""
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
        return bool(self.tavily_api_keys)

    @property
    def tavily_api_keys(self) -> list[str]:
        """Return all populated Tavily keys, in declaration order.
        Search uses these round-robin and falls through on per-key quota errors."""
        keys = [self.tavily_api_key, self.tavily_api_key_1,
                self.tavily_api_key_2, self.tavily_api_key_3]
        return [k for k in keys if k]

    @property
    def has_google_cse(self) -> bool:
        return bool(self.google_api_key and self.google_cse_id)

    @property
    def google_cse_active(self) -> bool:
        return self.has_google_cse and self.enable_google_cse

    @property
    def has_serper(self) -> bool:
        return bool(self.serper_api_keys)

    @property
    def serper_api_keys(self) -> list[str]:
        """Return all populated Serper keys, in declaration order.
        Search uses these round-robin and falls through on per-key 429/credit errors."""
        keys = [self.serper_api_key, self.serper_api_key_1, self.serper_api_key_2]
        return [k for k in keys if k]

    @property
    def has_brandfetch(self) -> bool:
        return bool(self.brandfetch_api_key)


settings = Settings()
