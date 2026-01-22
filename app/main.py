from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, settings as default_settings
from app.models.packets import (
    ActionPacket,
    Clarification,
    ClarificationAnswerRequest,
    IngestResponse,
    IntentPacket,
    Plan,
)
from app.normalization import (
    HttpProjectResolver,
    ProjectResolver,
    StubProjectResolver,
    apply_clarification_answer,
    normalize_intent,
)
from app.storage.db import (
    answer_clarification,
    check_db,
    create_clarification,
    create_db_engine,
    expire_clarification,
    get_clarification,
    get_intent,
    get_open_clarification_for_intent,
    insert_intent_artifact,
    list_open_clarifications,
    update_intent,
    upsert_intent_by_idempotency_key,
)
from app.util.canonical import canonical_json
from app.util.hashing import sha256_hex
from app.util.ids import new_correlation_id, new_intent_id


NOT_IMPLEMENTED_MESSAGE = "Phase 0: normalisation and execution are not implemented"


def create_app(app_settings: Settings | None = None) -> FastAPI:
    app_settings = app_settings or default_settings
    app = FastAPI()

    app.state.settings = app_settings
    app.state.engine = create_db_engine(app_settings.database_url)
    app.state.project_resolver = None

    def build_project_resolver(settings: Settings) -> ProjectResolver:
        if settings.context_api_base_url:
            return HttpProjectResolver(
                base_url=settings.context_api_base_url,
                bearer_token=settings.context_api_bearer_token,
                search_path=settings.context_api_project_search_path,
                timeout_seconds=settings.context_api_timeout_seconds,
            )
        return StubProjectResolver()

    app.state.project_resolver = build_project_resolver(app_settings)

    def get_settings() -> Settings:
        return app.state.settings

    def require_bearer(
        authorization: str | None = Header(default=None),
        settings: Settings = Depends(get_settings),
    ) -> None:
        if not authorization:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
        try:
            scheme, token = authorization.split(" ", 1)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization header")
        if scheme.lower() != "bearer" or token != settings.intent_service_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token")

    def persist_artifact(
        packet: Dict[str, Any],
        kind: str,
        intent_type: str | None,
        action: str | None,
        intent_id: str,
        correlation_id: str,
        status: str,
        idempotency_key: str | None,
        settings: Settings,
    ) -> None:
        event_time = datetime.now(timezone.utc)
        artifact = dict(packet)
        artifact.update(
            {
                "intent_id": intent_id,
                "correlation_id": correlation_id,
                "server_time": event_time.isoformat(),
            }
        )
        artifact_payload = {
            "intent_id": intent_id,
            "correlation_id": correlation_id,
            "supersedes_intent_id": None,
            "kind": kind,
            "intent_type": intent_type,
            "action": action,
            "status": status,
            "idempotency_key": idempotency_key,
            "artifact_version": settings.artifact_version,
            "artifact_hash": sha256_hex(canonical_json(artifact)),
            "artifact": artifact,
        }
        try:
            insert_intent_artifact(app.state.engine, artifact_payload)
        except SQLAlchemyError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")

    def compute_idempotency_key(packet: Dict[str, Any]) -> str:
        return f"intent:{sha256_hex(canonical_json(packet))}"

    def compute_action_idempotency_key(action: str, payload: Dict[str, Any]) -> str:
        return f"action:{sha256_hex(canonical_json({'action': action, 'payload': payload}))}"

    def build_plan(intent_id: str, correlation_id: str, final_canonical: Dict[str, Any]) -> Plan:
        intent_type = final_canonical.get("intent_type")
        fields = final_canonical.get("fields", {})
        if intent_type == "update_task":
            action_name = "notion.tasks.update"
            payload = {
                "notion_page_id": fields.get("task_id"),
                "patch": fields.get("patch", {}),
            }
        else:
            action_name = "notion.tasks.create"
            payload = fields
        action_packet = {
            "kind": "action",
            "action": action_name,
            "intent_id": intent_id,
            "correlation_id": correlation_id,
            "idempotency_key": compute_action_idempotency_key(action_name, payload),
            "payload": payload,
        }
        return Plan(actions=[ActionPacket.model_validate(action_packet)])

    def build_clarification_payload(row: Dict[str, Any]) -> Clarification:
        return Clarification(
            clarification_id=str(row["clarification_id"]),
            intent_id=row["intent_id"],
            question=row["question"],
            expected_answer_type=row["expected_answer_type"],
            candidates=row.get("candidates") or [],
            status=row["status"],
            answer=row.get("answer"),
            answered_at=row.get("answered_at").isoformat() if row.get("answered_at") else None,
        )

    def outcome_response_from_intent(
        intent_row: Dict[str, Any],
        clarification_row: Optional[Dict[str, Any]],
    ) -> IngestResponse:
        intent_status = intent_row["status"]
        intent_id = intent_row["intent_id"]
        correlation_id = intent_row["correlation_id"]
        if intent_status == "needs_clarification":
            clarification = build_clarification_payload(clarification_row) if clarification_row else None
            return IngestResponse(
                status="needs_clarification",
                intent_id=intent_id,
                correlation_id=correlation_id,
                clarification=clarification,
            )
        if intent_status == "ready":
            final_canonical = intent_row.get("final_canonical") or {}
            plan = build_plan(intent_id, correlation_id, final_canonical)
            return IngestResponse(
                status="ready",
                intent_id=intent_id,
                correlation_id=correlation_id,
                plan=plan,
            )
        if intent_status in {"failed", "expired"}:
            return IngestResponse(
                status="rejected",
                intent_id=intent_id,
                correlation_id=correlation_id,
                error_code="REJECTED",
                message="Intent rejected",
            )
        return IngestResponse(
            status="accepted",
            intent_id=intent_id,
            correlation_id=correlation_id,
            message="Intent accepted",
        )

    @app.get("/health")
    def health() -> Dict[str, str]:
        try:
            check_db(app.state.engine)
        except Exception:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")
        return {"status": "ok"}

    @app.get("/version")
    def version(settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
        return {
            "version": settings.version,
            "git_sha": settings.git_sha,
            "artifact_version": settings.artifact_version,
        }

    @app.post("/v1/intents", response_model=IngestResponse)
    def ingest_intent(
        packet: IntentPacket,
        response: Response,
        _: None = Depends(require_bearer),
        settings: Settings = Depends(get_settings),
        x_actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    ) -> IngestResponse:
        packet_data = packet.model_dump(mode="json", exclude_none=True)
        idempotency_key = compute_idempotency_key(packet_data)
        intent_id = packet.intent_id or new_intent_id()
        correlation_id = packet.correlation_id or new_correlation_id()
        actor_id = x_actor_id or packet_data.get("actor_id")
        packet_data["intent_id"] = intent_id
        packet_data["correlation_id"] = correlation_id
        if actor_id:
            packet_data["actor_id"] = actor_id

        try:
            intent_row, created = upsert_intent_by_idempotency_key(
                app.state.engine,
                intent_id=intent_id,
                idempotency_key=idempotency_key,
                status="received",
                raw_packet=packet_data,
                correlation_id=correlation_id,
                actor_id=actor_id,
            )
        except SQLAlchemyError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")

        intent_id = intent_row["intent_id"]
        correlation_id = intent_row["correlation_id"]
        packet_data["intent_id"] = intent_id
        packet_data["correlation_id"] = correlation_id

        try:
            persist_artifact(
                packet=packet_data,
                kind="intent",
                intent_type=packet.intent_type,
                action=None,
                intent_id=intent_id,
                correlation_id=correlation_id,
                status="received",
                idempotency_key=idempotency_key,
                settings=settings,
            )
        except SQLAlchemyError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")

        response.headers["X-Intent-Id"] = intent_id
        response.headers["X-Correlation-Id"] = correlation_id

        if not created:
            clarification_row = None
            if intent_row["status"] == "needs_clarification":
                clarification_row = get_open_clarification_for_intent(
                    app.state.engine,
                    intent_id,
                    actor_id=actor_id,
                    expiry_hours=settings.clarification_expiry_hours,
                )
            response_payload = outcome_response_from_intent(intent_row, clarification_row)
            try:
                persist_artifact(
                    packet=response_payload.model_dump(mode="json", exclude_none=True),
                    kind="intent",
                    intent_type=packet.intent_type,
                    action=None,
                    intent_id=intent_id,
                    correlation_id=correlation_id,
                    status=response_payload.status,
                    idempotency_key=idempotency_key,
                    settings=settings,
                )
            except SQLAlchemyError:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")
            return response_payload

        result = normalize_intent(
            packet_data,
            user_timezone=settings.user_timezone,
            resolver=app.state.project_resolver,
            project_resolution_threshold=settings.project_resolution_threshold,
            project_resolution_margin=settings.project_resolution_margin,
            min_confidence_to_write=settings.min_confidence_to_write,
            max_inferred_fields=settings.max_inferred_fields,
        )
        if result.status == "needs_clarification":
            clarification_row = create_clarification(
                app.state.engine,
                intent_id=intent_id,
                status="open",
                question=result.clarification.question if result.clarification else "Clarification required",
                expected_answer_type=result.clarification.expected_answer_type if result.clarification else "free_text",
                candidates=result.clarification.candidates if result.clarification else [],
                actor_id=actor_id,
            )
            intent_row = update_intent(
                app.state.engine,
                intent_id=intent_id,
                status="needs_clarification",
                canonical_draft=result.canonical_draft or {},
                actor_id=actor_id,
            )
            clarification_payload = build_clarification_payload(clarification_row)
            response_payload = IngestResponse(
                status="needs_clarification",
                intent_id=intent_id,
                correlation_id=correlation_id,
                clarification=clarification_payload,
            )
            persist_artifact(
                packet={
                    "status": "needs_clarification",
                    "clarification": response_payload.clarification.model_dump(mode="json"),
                    "canonical_draft": intent_row.get("canonical_draft"),
                },
                kind="intent",
                intent_type=packet.intent_type,
                action=None,
                intent_id=intent_id,
                correlation_id=correlation_id,
                status="needs_clarification",
                idempotency_key=idempotency_key,
                settings=settings,
            )
            return response_payload

        if result.status == "ready":
            intent_row = update_intent(
                app.state.engine,
                intent_id=intent_id,
                status="ready",
                canonical_draft=result.canonical_draft or {},
                final_canonical=result.final_canonical or {},
            )
            plan = build_plan(intent_id, correlation_id, intent_row.get("final_canonical") or {})
            response_payload = IngestResponse(
                status="ready",
                intent_id=intent_id,
                correlation_id=correlation_id,
                plan=plan,
            )
            persist_artifact(
                packet={
                    "status": "ready",
                    "final_canonical": intent_row.get("final_canonical"),
                    "plan": response_payload.plan.model_dump(mode="json"),
                },
                kind="intent",
                intent_type=packet.intent_type,
                action=None,
                intent_id=intent_id,
                correlation_id=correlation_id,
                status="ready",
                idempotency_key=idempotency_key,
                settings=settings,
            )
            return response_payload

        intent_row = update_intent(app.state.engine, intent_id=intent_id, status="failed")
        response_payload = IngestResponse(
            status="rejected",
            error_code=result.error_code or "REJECTED",
            message=result.message or "Intent rejected",
            details=result.details,
            intent_id=intent_id,
            correlation_id=correlation_id,
        )
        persist_artifact(
            packet=response_payload.model_dump(mode="json", exclude_none=True),
            kind="intent",
            intent_type=packet.intent_type,
            action=None,
            intent_id=intent_id,
            correlation_id=correlation_id,
            status="rejected",
            idempotency_key=idempotency_key,
            settings=settings,
        )
        return response_payload

    @app.post("/v1/actions", response_model=IngestResponse)
    def ingest_action(
        packet: ActionPacket,
        response: Response,
        _: None = Depends(require_bearer),
        settings: Settings = Depends(get_settings),
    ) -> IngestResponse:
        intent_id = packet.intent_id or new_intent_id()
        correlation_id = packet.correlation_id or new_correlation_id()
        packet_data = packet.model_dump(mode="json", exclude_none=True)
        packet_data["intent_id"] = intent_id
        packet_data["correlation_id"] = correlation_id

        try:
            persist_artifact(
                packet=packet_data,
                kind="action",
                intent_type=None,
                action=packet.action,
                intent_id=intent_id,
                correlation_id=correlation_id,
                status="received",
                idempotency_key=None,
                settings=settings,
            )
        except SQLAlchemyError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")

        response.headers["X-Intent-Id"] = intent_id
        response.headers["X-Correlation-Id"] = correlation_id
        if not packet.action:
            response_payload = IngestResponse(
                status="rejected",
                error_code="VALIDATION_ERROR",
                message="Missing action",
                intent_id=intent_id,
                correlation_id=correlation_id,
            )
        else:
            response_payload = IngestResponse(
                status="accepted",
                message=NOT_IMPLEMENTED_MESSAGE,
                intent_id=intent_id,
                correlation_id=correlation_id,
            )
        persist_artifact(
            packet=response_payload.model_dump(mode="json", exclude_none=True),
            kind="action",
            intent_type=None,
            action=packet.action,
            intent_id=intent_id,
            correlation_id=correlation_id,
            status=response_payload.status,
            idempotency_key=None,
            settings=settings,
        )
        return response_payload

    @app.get("/v1/clarifications", response_model=List[Clarification])
    def list_clarifications(
        status: str = "open",
        _: None = Depends(require_bearer),
        settings: Settings = Depends(get_settings),
        x_actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    ) -> List[Clarification]:
        if status != "open":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported status filter")
        rows = list_open_clarifications(
            app.state.engine,
            actor_id=x_actor_id,
            expiry_hours=settings.clarification_expiry_hours,
        )
        return [build_clarification_payload(row) for row in rows]

    @app.post("/v1/clarifications/{clarification_id}/answer", response_model=IngestResponse)
    def answer_clarification_endpoint(
        clarification_id: str,
        payload: ClarificationAnswerRequest,
        _: None = Depends(require_bearer),
        settings: Settings = Depends(get_settings),
        x_actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    ) -> IngestResponse:
        if not payload.choice_id and not payload.answer_text:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Answer payload required")
        clarification = get_clarification(app.state.engine, clarification_id)
        if not clarification:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clarification not found")
        if x_actor_id and clarification.get("actor_id") and clarification.get("actor_id") != x_actor_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clarification not found")
        if clarification["status"] == "open":
            created_at = clarification.get("created_at")
            if created_at:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.clarification_expiry_hours)
                if created_at < cutoff:
                    expire_clarification(app.state.engine, clarification_id=clarification_id)
                    update_intent(app.state.engine, intent_id=clarification["intent_id"], status="expired")
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Clarification expired")
        if clarification["status"] != "open":
            if clarification["status"] == "answered":
                stored_answer = clarification.get("answer") or {}
                if stored_answer == payload.model_dump(mode="json", exclude_none=True):
                    intent_row = get_intent(app.state.engine, clarification["intent_id"])
                    if not intent_row:
                        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Intent not found")
                    clarification_row = None
                    if intent_row["status"] == "needs_clarification":
                        clarification_row = get_open_clarification_for_intent(
                            app.state.engine,
                            intent_row["intent_id"],
                            actor_id=x_actor_id,
                            expiry_hours=settings.clarification_expiry_hours,
                        )
                    return outcome_response_from_intent(intent_row, clarification_row)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Clarification already answered")

        answer_payload = payload.model_dump(mode="json", exclude_none=True)
        answered = answer_clarification(
            app.state.engine,
            clarification_id=clarification_id,
            answer_payload=answer_payload,
        )
        if not answered:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Clarification already answered")

        intent_row = get_intent(app.state.engine, clarification["intent_id"])
        if not intent_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Intent not found")

        persist_artifact(
            packet={
                "clarification_id": clarification_id,
                "intent_id": intent_row["intent_id"],
                "answer": answer_payload,
            },
            kind="intent",
            intent_type=None,
            action=None,
            intent_id=intent_row["intent_id"],
            correlation_id=intent_row["correlation_id"],
            status="clarification_answered",
            idempotency_key=None,
            settings=settings,
        )

        canonical_draft = intent_row.get("canonical_draft") or {}
        updated_packet = apply_clarification_answer(canonical_draft, answer_payload)
        updated_packet["intent_id"] = intent_row["intent_id"]
        updated_packet["correlation_id"] = intent_row["correlation_id"]

        result = normalize_intent(
            updated_packet,
            user_timezone=settings.user_timezone,
            resolver=app.state.project_resolver,
            project_resolution_threshold=settings.project_resolution_threshold,
            project_resolution_margin=settings.project_resolution_margin,
            min_confidence_to_write=settings.min_confidence_to_write,
            max_inferred_fields=settings.max_inferred_fields,
        )

        if result.status == "needs_clarification":
            # Create a fresh clarification to preserve append-only history.
            clarification_row = create_clarification(
                app.state.engine,
                intent_id=intent_row["intent_id"],
                status="open",
                question=result.clarification.question if result.clarification else "Clarification required",
                expected_answer_type=result.clarification.expected_answer_type if result.clarification else "free_text",
                candidates=result.clarification.candidates if result.clarification else [],
                actor_id=intent_row.get("actor_id"),
            )
            intent_row = update_intent(
                app.state.engine,
                intent_id=intent_row["intent_id"],
                status="needs_clarification",
                canonical_draft=result.canonical_draft or updated_packet,
                actor_id=intent_row.get("actor_id"),
            )
            clarification_payload = build_clarification_payload(clarification_row)
            response_payload = IngestResponse(
                status="needs_clarification",
                intent_id=intent_row["intent_id"],
                correlation_id=intent_row["correlation_id"],
                clarification=clarification_payload,
            )
            persist_artifact(
                packet={
                    "status": "needs_clarification",
                    "clarification": response_payload.clarification.model_dump(mode="json"),
                    "canonical_draft": intent_row.get("canonical_draft"),
                },
                kind="intent",
                intent_type=None,
                action=None,
                intent_id=intent_row["intent_id"],
                correlation_id=intent_row["correlation_id"],
                status="needs_clarification",
                idempotency_key=None,
                settings=settings,
            )
            return response_payload

        if result.status == "ready":
            intent_row = update_intent(
                app.state.engine,
                intent_id=intent_row["intent_id"],
                status="ready",
                canonical_draft=result.canonical_draft or updated_packet,
                final_canonical=result.final_canonical or {},
            )
            plan = build_plan(intent_row["intent_id"], intent_row["correlation_id"], intent_row.get("final_canonical") or {})
            response_payload = IngestResponse(
                status="ready",
                intent_id=intent_row["intent_id"],
                correlation_id=intent_row["correlation_id"],
                plan=plan,
            )
            persist_artifact(
                packet={
                    "status": "ready",
                    "final_canonical": intent_row.get("final_canonical"),
                    "plan": response_payload.plan.model_dump(mode="json"),
                },
                kind="intent",
                intent_type=None,
                action=None,
                intent_id=intent_row["intent_id"],
                correlation_id=intent_row["correlation_id"],
                status="ready",
                idempotency_key=None,
                settings=settings,
            )
            return response_payload

        intent_row = update_intent(app.state.engine, intent_id=intent_row["intent_id"], status="failed")
        response_payload = IngestResponse(
            status="rejected",
            error_code=result.error_code or "REJECTED",
            message=result.message or "Intent rejected",
            details=result.details,
            intent_id=intent_row["intent_id"],
            correlation_id=intent_row["correlation_id"],
        )
        persist_artifact(
            packet=response_payload.model_dump(mode="json", exclude_none=True),
            kind="intent",
            intent_type=None,
            action=None,
            intent_id=intent_row["intent_id"],
            correlation_id=intent_row["correlation_id"],
            status="rejected",
            idempotency_key=None,
            settings=settings,
        )
        return response_payload

    @app.get("/v1/intents/{intent_id}", response_model=IngestResponse)
    def get_intent_endpoint(
        intent_id: str,
        _: None = Depends(require_bearer),
        settings: Settings = Depends(get_settings),
        x_actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    ) -> IngestResponse:
        intent_row = get_intent(app.state.engine, intent_id)
        if not intent_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Intent not found")
        if x_actor_id and intent_row.get("actor_id") and intent_row.get("actor_id") != x_actor_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Intent not found")
        clarification_row = None
        if intent_row["status"] == "needs_clarification":
            clarification_row = get_open_clarification_for_intent(
                app.state.engine,
                intent_id,
                actor_id=x_actor_id,
                expiry_hours=settings.clarification_expiry_hours,
            )
        return outcome_response_from_intent(intent_row, clarification_row)

    return app


app = create_app()
