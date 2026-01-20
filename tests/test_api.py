from __future__ import annotations

import os

import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def build_settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        intent_service_token=os.environ.get("INTENT_SERVICE_TOKEN", "change-me"),
        user_timezone="Europe/London",
        min_confidence_to_write=0.75,
        max_inferred_fields=2,
        execute_actions=False,
        version="0.0.0",
        git_sha="test",
        artifact_version=1,
    )


def test_auth_required() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    response = client.post("/v1/ingest/intent", json={"kind": "intent"})

    assert response.status_code == 401


def test_ingest_writes_artifact_row() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    headers = {"Authorization": f"Bearer {settings.intent_service_token}"}
    response = client.post("/v1/ingest/intent", json={"kind": "intent", "intent_type": "noop"}, headers=headers)

    assert response.status_code == 200
    data = response.json()
    intent_id = data["intent_id"]

    engine = sa.create_engine(settings.database_url, future=True)
    with engine.connect() as conn:
        count = conn.execute(sa.text("SELECT count(*) FROM intent_artifacts WHERE intent_id = :intent_id"), {"intent_id": intent_id}).scalar_one()

    assert count == 1


def test_health_returns_503_when_db_unavailable() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://invalid:invalid@127.0.0.1:1/invalid?connect_timeout=1",
        intent_service_token="change-me",
    )
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 503
