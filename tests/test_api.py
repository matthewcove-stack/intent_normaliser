from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import pytest

import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.config import Settings
import app.main as main
from app.main import create_app
from app.normalization import NormalizationResult
from app.util.idempotency import compute_idempotency_key


def build_settings(**overrides: Any) -> Settings:
    values: Dict[str, Any] = {
        "database_url": os.environ["DATABASE_URL"],
        "intent_service_token": os.environ.get("INTENT_SERVICE_TOKEN", "change-me"),
        "user_timezone": "Europe/London",
        "min_confidence_to_write": 0.75,
        "max_inferred_fields": 2,
        "execute_actions": False,
        "clarification_expiry_hours": 72,
        "project_resolution_threshold": 0.90,
        "project_resolution_margin": 0.10,
        "version": "0.0.0",
        "git_sha": "test",
        "artifact_version": 1,
    }
    values.update(overrides)
    return Settings(**values)


def assert_receipt_fields(payload: Dict[str, Any]) -> None:
    assert payload["receipt_id"]
    assert payload["trace_id"]
    assert payload["idempotency_key"]


@pytest.fixture(autouse=True)
def reset_database() -> None:
    engine = sa.create_engine(os.environ["DATABASE_URL"], future=True)
    with engine.begin() as conn:
        conn.execute(sa.text("TRUNCATE intent_artifacts, clarifications, intents RESTART IDENTITY CASCADE"))


class DummyResponse:
    def __init__(self, status_code: int, json_data: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("No JSON")
        return self._json_data


class DummyClient:
    def __init__(self, responses: List[DummyResponse], calls: List[Dict[str, Any]]) -> None:
        self._responses = responses
        self._calls = calls

    def __enter__(self) -> "DummyClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, url: str, json: Dict[str, Any], headers: Dict[str, str]) -> DummyResponse:
        self._calls.append({"url": url, "json": json, "headers": headers})
        if not self._responses:
            raise AssertionError("Gateway called more times than expected")
        return self._responses.pop(0)


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
    assert_receipt_fields(data)
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
        "request_id": "req-clarify-001",
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
        "request_id": "req-clarify-002",
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
    assert action["payload"]["project"] == "proj_123"

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
    assert response_one.json()["receipt_id"] == response_two.json()["receipt_id"]

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
        "request_id": "req-clarify-003",
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

    headers = {
        "Authorization": f"Bearer {settings.intent_service_token}",
        "X-Actor-Id": "actor-expire-1",
    }
    payload = {
        "kind": "intent",
        "intent_type": "create_task",
        "fields": {"title": "Old task", "project": "John and Sagita"},
        "request_id": "req-clarify-004",
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


def test_execute_actions_happy_path_persists_artifacts(monkeypatch) -> None:
    settings = build_settings(
        execute_actions=True,
        gateway_base_url="http://gateway",
        gateway_bearer_token="token",
    )
    responses = [
        DummyResponse(
            200,
            json_data={
                "request_id": "req-1",
                "status": "ok",
                "data": {"notion_page_id": "notion_123"},
            },
            text='{"status":"ok"}',
        )
    ]
    calls: List[Dict[str, Any]] = []

    def client_factory(*args: Any, **kwargs: Any) -> DummyClient:
        return DummyClient(responses, calls)

    monkeypatch.setattr(main.httpx, "Client", client_factory)

    app = create_app(settings)
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {settings.intent_service_token}",
        "X-Actor-Id": "actor-expire-1",
    }
    payload = {
        "kind": "intent",
        "intent_type": "create_task",
        "fields": {"title": "Ship this"},
        "request_id": "req-1",
    }

    response = client.post("/v1/intents", json=payload, headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert_receipt_fields(data)
    assert data["status"] == "executed"
    assert data["details"]["notion_task_id"] == "notion_123"
    assert data["details"]["request_id"] == "req-1"
    assert len(calls) == 1

    engine = sa.create_engine(settings.database_url, future=True)
    with engine.connect() as conn:
        action_count = conn.execute(
            sa.text(
                "SELECT count(*) FROM intent_artifacts WHERE intent_id = :intent_id AND kind = 'action'"
            ),
            {"intent_id": data["intent_id"]},
        ).scalar_one()
        executed_count = conn.execute(
            sa.text(
                "SELECT count(*) FROM intent_artifacts WHERE intent_id = :intent_id AND status = 'executed'"
            ),
            {"intent_id": data["intent_id"]},
        ).scalar_one()

    assert action_count >= 1
    assert executed_count >= 1


def test_execution_idempotency_skips_gateway(monkeypatch) -> None:
    settings = build_settings(
        execute_actions=True,
        gateway_base_url="http://gateway",
        gateway_bearer_token="token",
    )
    responses = [
        DummyResponse(
            200,
            json_data={
                "request_id": "req-dup",
                "status": "ok",
                "data": {"notion_page_id": "notion_dup"},
            },
            text='{"status":"ok"}',
        )
    ]
    calls: List[Dict[str, Any]] = []

    def client_factory(*args: Any, **kwargs: Any) -> DummyClient:
        return DummyClient(responses, calls)

    monkeypatch.setattr(main.httpx, "Client", client_factory)

    app = create_app(settings)
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {settings.intent_service_token}",
        "X-Actor-Id": "actor-expire-1",
    }
    payload = {
        "kind": "intent",
        "intent_type": "create_task",
        "fields": {"title": "Do the thing"},
        "request_id": "req-dup",
    }

    first = client.post("/v1/intents", json=payload, headers=headers)
    second = client.post("/v1/intents", json=payload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "executed"
    assert second.json()["status"] == "executed"
    assert first.json()["details"]["notion_task_id"] == "notion_dup"
    assert second.json()["details"]["notion_task_id"] == "notion_dup"
    assert first.json()["receipt_id"] == second.json()["receipt_id"]
    assert first.json()["idempotency_key"] == second.json()["idempotency_key"]
    assert len(calls) == 1


def test_execute_actions_failure_returns_error(monkeypatch) -> None:
    settings = build_settings(
        execute_actions=True,
        gateway_base_url="http://gateway",
        gateway_bearer_token="token",
    )
    responses = [
        DummyResponse(
            500,
            json_data={
                "request_id": "req-fail",
                "status": "error",
                "error": {"code": "tasks_create_failed", "message": "boom"},
            },
            text='{"status":"error"}',
        )
    ]
    calls: List[Dict[str, Any]] = []

    def client_factory(*args: Any, **kwargs: Any) -> DummyClient:
        return DummyClient(responses, calls)

    monkeypatch.setattr(main.httpx, "Client", client_factory)

    app = create_app(settings)
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {settings.intent_service_token}",
        "X-Actor-Id": "actor-expire-1",
    }
    payload = {
        "kind": "intent",
        "intent_type": "create_task",
        "fields": {"title": "Fail this"},
        "request_id": "req-fail",
    }

    response = client.post("/v1/intents", json=payload, headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert_receipt_fields(data)
    assert data["status"] == "failed"
    assert data["error"]["code"] == "tasks_create_failed"
    assert data["error"]["details"]["status_code"] == 500
    assert len(calls) == 1


def test_idempotency_canonicalization_stable() -> None:
    payload_one = json.loads('{"kind":"intent","natural_language":"Draft plan","fields":{"a":1,"b":2}}')
    payload_two = json.loads(
        '{ "fields": { "b": 2, "a": 1 }, "natural_language": "Draft plan", "kind": "intent" }'
    )

    key_one = compute_idempotency_key(payload_one)
    key_two = compute_idempotency_key(payload_two)

    assert key_one == key_two


def test_persist_first_before_normalize(monkeypatch) -> None:
    settings = build_settings()
    app = create_app(settings)
    client = TestClient(app)

    calls: List[str] = []
    original_upsert = main.upsert_intent_by_idempotency_key

    def tracked_upsert(*args: Any, **kwargs: Any):
        calls.append("persist")
        return original_upsert(*args, **kwargs)

    def fake_normalize(*args: Any, **kwargs: Any) -> NormalizationResult:
        assert calls == ["persist"]
        return NormalizationResult(
            status="ready",
            canonical_draft={"intent_type": "create_task", "fields": {"title": "Test"}},
            final_canonical={"intent_type": "create_task", "fields": {"title": "Test"}},
        )

    monkeypatch.setattr(main, "upsert_intent_by_idempotency_key", tracked_upsert)
    monkeypatch.setattr(main, "normalize_intent", fake_normalize)

    headers = {"Authorization": f"Bearer {settings.intent_service_token}"}
    payload = {"kind": "intent", "intent_type": "create_task", "fields": {"title": "Test"}}

    response = client.post("/v1/intents", json=payload, headers=headers)

    assert response.status_code == 200
    assert calls == ["persist"]
