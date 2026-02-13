from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    intent_service_token: str
    user_timezone: str = "Europe/London"
    min_confidence_to_write: float = 0.75
    max_inferred_fields: int = 2
    execute_actions: bool = False
    gateway_base_url: str | None = None
    gateway_bearer_token: str | None = None
    gateway_tasks_create_path: str = "/v1/notion/tasks/create"
    gateway_tasks_update_path: str = "/v1/notion/tasks/update"
    gateway_lists_add_item_path: str = "/v1/notion/lists/add_item"
    gateway_notes_capture_path: str = "/v1/notion/notes/capture"
    gateway_timeout_seconds: float = 15.0
    context_api_base_url: str | None = None
    context_api_bearer_token: str | None = None
    context_api_project_search_path: str = "/v1/projects/search"
    context_api_timeout_seconds: float = 5.0
    clarification_expiry_hours: int = 72
    project_resolution_threshold: float = 0.90
    project_resolution_margin: float = 0.10
    version: str = "0.0.0"
    git_sha: str = "unknown"
    artifact_version: int = 1

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()
