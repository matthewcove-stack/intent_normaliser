from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    intent_service_token: str
    user_timezone: str = "Europe/London"
    min_confidence_to_write: float = 0.75
    max_inferred_fields: int = 2
    execute_actions: bool = False
    version: str = "0.0.0"
    git_sha: str = "unknown"
    artifact_version: int = 1

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()
