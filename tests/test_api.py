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
        clarification_expiry_hours=72,
        project_resolution_threshold=0.90,
        project_resolution_margin=0.10,
        version="0.0.0",
        git_sha="test",
        artifact_version=1,
    )


def test_auth_required() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    response = client.post("/v1/intents", json={"kind": "intent"})

    assert response.status_code == 401


def test_ingest_writes_artifact_row() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    headers = {"Authorization": f"Bearer {settings.intent_service_token}"}
    response = client.post("/v1/intents", json={"kind": "intent", "intent_type": "noop"}, headers=headers)

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
    response = client.post("/v1/intents", json=payload, headers=headers)

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
    response = client.post("/v1/intents", json=payload, headers=headers)
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
    action = data["plan"]["actions"][0]
    assert action["action"] == "notion.tasks.create"
    assert "payload" in action
    assert action["payload"]["project_id"] == "proj_123"

    engine = sa.create_engine(settings.database_url, future=True)
    with engine.connect() as conn:
        intent_status = conn.execute(
            sa.text("SELECT status FROM intents WHERE intent_id = :intent_id"),
            {"intent_id": intent_id},
        ).scalar_one()
        answered_count = conn.execute(
            sa.text(
                "SELECT count(*) FROM intent_artifacts WHERE intent_id = :intent_id AND status = 'clarification_answered'"
            ),
            {"intent_id": intent_id},
        ).scalar_one()
        ready_count = conn.execute(
            sa.text(
                "SELECT count(*) FROM intent_artifacts WHERE intent_id = :intent_id AND status = 'ready'"
            ),
            {"intent_id": intent_id},
        ).scalar_one()

    assert intent_status == "ready"
    assert answered_count >= 1
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
    response_one = client.post("/v1/intents", json=payload, headers=headers)
    response_two = client.post("/v1/intents", json=payload, headers=headers)

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


def test_idempotent_clarification_answer_returns_ready() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    headers = {"Authorization": f"Bearer {settings.intent_service_token}"}
    payload = {
        "kind": "intent",
        "intent_type": "create_task",
        "fields": {"title": "Ship this", "project": "John and Sagita"},
    }
    response = client.post("/v1/intents", json=payload, headers=headers)
    clarification_id = response.json()["clarification"]["clarification_id"]

    answer_payload = {"choice_id": "proj_123"}
    first = client.post(
        f"/v1/clarifications/{clarification_id}/answer",
        json=answer_payload,
        headers=headers,
    )
    assert first.status_code == 200
    assert first.json()["status"] == "ready"

    second = client.post(
        f"/v1/clarifications/{clarification_id}/answer",
        json=answer_payload,
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["status"] == "ready"


def test_expired_clarification_is_removed() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    headers = {"Authorization": f"Bearer {settings.intent_service_token}"}
    payload = {
        "kind": "intent",
        "intent_type": "create_task",
        "fields": {"title": "Old task", "project": "John and Sagita"},
    }
    response = client.post("/v1/intents", json=payload, headers=headers)
    intent_id = response.json()["intent_id"]

    engine = sa.create_engine(settings.database_url, future=True)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE clarifications SET created_at = now() - interval '200 hours' WHERE intent_id = :intent_id"),
            {"intent_id": intent_id},
        )

    list_response = client.get("/v1/clarifications", headers=headers)
    assert list_response.status_code == 200
    assert list_response.json() == []

    with engine.connect() as conn:
        status_value = conn.execute(
            sa.text("SELECT status FROM intents WHERE intent_id = :intent_id"),
            {"intent_id": intent_id},
        ).scalar_one()
    assert status_value == "expired"


def test_low_confidence_rejected() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    headers = {"Authorization": f"Bearer {settings.intent_service_token}"}
    payload = {
        "kind": "intent",
        "intent_type": "create_task",
        "confidence": 0.1,
        "fields": {"title": "Low confidence task"},
    }
    response = client.post("/v1/intents", json=payload, headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "rejected"
    assert data["error_code"] == "POLICY_LOW_CONFIDENCE"


def test_update_task_requires_task_id() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    headers = {"Authorization": f"Bearer {settings.intent_service_token}"}
    payload = {
        "kind": "intent",
        "intent_type": "update_task",
        "fields": {"status": "In Progress"},
    }
    response = client.post("/v1/intents", json=payload, headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "rejected"
    assert data["error_code"] == "POLICY_MISSING_TASK_ID"


def test_update_task_ready_returns_patch_payload() -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    headers = {"Authorization": f"Bearer {settings.intent_service_token}"}
    payload = {
        "kind": "intent",
        "intent_type": "update_task",
        "fields": {"task_id": "task_123", "status": "In Progress"},
    }
    response = client.post("/v1/intents", json=payload, headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    action = data["plan"]["actions"][0]
    assert action["action"] == "notion.tasks.update"
    assert action["payload"]["notion_page_id"] == "task_123"
    assert action["payload"]["patch"]["status"] == "In Progress"
