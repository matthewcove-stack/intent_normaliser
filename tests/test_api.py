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
        count = conn.execute(
            sa.text("SELECT count(*) FROM intent_artifacts WHERE intent_id = :intent_id"),
            {"intent_id": intent_id},
        ).scalar_one()

    assert count >= 2


def test_health_returns_503_when_db_unavailable() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://invalid:invalid@127.0.0.1:1/invalid?connect_timeout=1",
        intent_service_token="change-me",
    )
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 503


def test_ingest_intent_creates_open_clarification_when_project_string_present() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    headers = {"Authorization": f"Bearer {settings.intent_service_token}"}
    payload = {
        "kind": "intent",
        "intent_type": "create_task",
        "fields": {"title": "Write the spec", "project": "John and Sagita"},
    }
    response = client.post("/v1/ingest/intent", json=payload, headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "needs_clarification"
    assert data["clarification"]["clarification_id"]

    intent_id = data["intent_id"]
    engine = sa.create_engine(settings.database_url, future=True)
    with engine.connect() as conn:
        artifact_count = conn.execute(
            sa.text("SELECT count(*) FROM intent_artifacts WHERE intent_id = :intent_id"),
            {"intent_id": intent_id},
        ).scalar_one()
        clarifications = conn.execute(
            sa.text(
                "SELECT count(*) FROM clarifications WHERE intent_id = :intent_id AND status = 'open'"
            ),
            {"intent_id": intent_id},
        ).scalar_one()

    assert artifact_count >= 2
    assert clarifications == 1


def test_answer_clarification_by_choice_id_resumes_to_ready() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    headers = {"Authorization": f"Bearer {settings.intent_service_token}"}
    payload = {
        "kind": "intent",
        "intent_type": "create_task",
        "fields": {"title": "Ship this", "project": "John and Sagita"},
    }
    response = client.post("/v1/ingest/intent", json=payload, headers=headers)
    clarification_id = response.json()["clarification"]["clarification_id"]
    intent_id = response.json()["intent_id"]

    answer_response = client.post(
        f"/v1/clarifications/{clarification_id}/answer",
        json={"choice_id": "proj_123"},
        headers=headers,
    )

    assert answer_response.status_code == 200
    data = answer_response.json()
    assert data["status"] == "ready"
    assert data["plan"]["actions"]

    engine = sa.create_engine(settings.database_url, future=True)
    with engine.connect() as conn:
        intent_status = conn.execute(
            sa.text("SELECT status FROM intents WHERE intent_id = :intent_id"),
            {"intent_id": intent_id},
        ).scalar_one()
        ready_count = conn.execute(
            sa.text(
                "SELECT count(*) FROM intent_artifacts WHERE intent_id = :intent_id AND status = 'ready'"
            ),
            {"intent_id": intent_id},
        ).scalar_one()

    assert intent_status == "ready"
    assert ready_count >= 1


def test_idempotent_repost_returns_same_intent() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    headers = {"Authorization": f"Bearer {settings.intent_service_token}"}
    payload = {
        "kind": "intent",
        "intent_type": "create_task",
        "fields": {"title": "Cleanup", "project": "John and Sagita"},
    }
    response_one = client.post("/v1/ingest/intent", json=payload, headers=headers)
    response_two = client.post("/v1/ingest/intent", json=payload, headers=headers)

    assert response_one.status_code == 200
    assert response_two.status_code == 200
    intent_id_one = response_one.json()["intent_id"]
    intent_id_two = response_two.json()["intent_id"]
    assert intent_id_one == intent_id_two

    engine = sa.create_engine(settings.database_url, future=True)
    with engine.connect() as conn:
        clarifications = conn.execute(
            sa.text(
                "SELECT count(*) FROM clarifications WHERE intent_id = :intent_id AND status = 'open'"
            ),
            {"intent_id": intent_id_one},
        ).scalar_one()

    assert clarifications == 1
