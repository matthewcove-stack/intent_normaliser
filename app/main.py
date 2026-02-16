from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
from pydantic import ValidationError
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
    get_latest_intent_artifact,
    get_intent,
    get_open_clarification_for_intent,
    insert_intent_artifact,
    list_open_clarifications,
    update_intent,
    upsert_intent_by_idempotency_key,
)
from app.util.canonical import canonical_json
from app.util.hashing import sha256_hex
from app.util.idempotency import compute_idempotency_key
from app.util.ids import new_correlation_id, new_intent_id, new_trace_id


NOT_IMPLEMENTED_MESSAGE = "Phase 0: normalisation and execution are not implemented"

logger = logging.getLogger("intent_normaliser")


def build_error_payload(
    code: str,
    message: str,
    status_code: int | None = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {"code": code, "message": message}
    if status_code is not None or details:
        detail_payload = dict(details or {})
        if status_code is not None:
            detail_payload.setdefault("status_code", status_code)
        payload["details"] = detail_payload
    return payload


def build_error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": build_error_payload(code, message, status_code=status_code, details=details)},
    )


def create_app(app_settings: Settings | None = None) -> FastAPI:
    app_settings = app_settings or default_settings
    app = FastAPI()
    cors_origins = [origin.strip() for origin in app_settings.intent_cors_origins.split(",") if origin.strip()]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

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

    def compute_request_id(packet: Dict[str, Any], idempotency_key: str) -> tuple[str, bool]:
        for key in ("request_id", "requestId"):
            value = packet.get(key)
            if value:
                return str(value), True
        return f"intent:{idempotency_key}", False

    def compute_action_idempotency_key(action: str, payload: Dict[str, Any]) -> str:
        return f"action:{sha256_hex(canonical_json({'action': action, 'payload': payload}))}"

    def build_gateway_request(
        action_packet: ActionPacket,
        actor_id: Optional[str],
        request_id: Optional[str],
        settings: Settings,
    ) -> tuple[str, Dict[str, Any]]:
        action_name = action_packet.action or ""
        payload = action_packet.payload or {}
        if action_name == "notion.tasks.create":
            endpoint = settings.gateway_tasks_create_path
            gateway_payload = {"task": payload}
        elif action_name == "notion.tasks.update":
            endpoint = settings.gateway_tasks_update_path
            gateway_payload = payload
            if not isinstance(gateway_payload, dict) or not gateway_payload.get("notion_page_id"):
                raise ValueError("Missing notion_page_id for update")
        elif action_name == "notion.list.add_item":
            endpoint = settings.gateway_lists_add_item_path
            gateway_payload = {"list_item": payload}
        elif action_name == "notion.note.capture":
            endpoint = settings.gateway_notes_capture_path
            gateway_payload = {"note": payload}
        else:
            raise ValueError(f"Unsupported action: {action_name}")
        idempotency_key = action_packet.idempotency_key or compute_action_idempotency_key(action_name, payload)
        request_value = request_id or str(uuid.uuid4())
        envelope = {
            "request_id": request_value,
            "idempotency_key": idempotency_key,
            "actor": actor_id or "intent_normaliser",
            "payload": gateway_payload,
        }
        return endpoint, envelope

    def execute_plan(
        intent_id: str,
        correlation_id: str,
        actor_id: Optional[str],
        request_id: Optional[str],
        plan: Plan,
        settings: Settings,
    ) -> tuple[bool, list[Dict[str, Any]]]:
        if not settings.gateway_base_url or not settings.gateway_bearer_token:
            raise ValueError("Gateway execution not configured")
        results: list[Dict[str, Any]] = []
        headers = {"Authorization": f"Bearer {settings.gateway_bearer_token}", "Content-Type": "application/json"}
        base_url = settings.gateway_base_url.rstrip("/")
        with httpx.Client(timeout=settings.gateway_timeout_seconds) as client:
            for action in plan.actions:
                action_name = action.action or ""
                success = False
                response_body = None
                response_json: Optional[Dict[str, Any]] = None
                status_code = None
                error_message = None
                error_code = None
                notion_task_id = None
                request_envelope: Dict[str, Any] = {}
                endpoint = ""
                try:
                    endpoint, request_envelope = build_gateway_request(action, actor_id, request_id, settings)
                    url = f"{base_url}{endpoint}"
                    response = client.post(url, json=request_envelope, headers=headers)
                    status_code = response.status_code
                    response_body = response.text
                    try:
                        response_json = response.json()
                    except ValueError:
                        response_json = None
                    success = 200 <= response.status_code < 300
                    if response_json:
                        error_payload = response_json.get("error") if isinstance(response_json, dict) else None
                        if error_payload:
                            error_code = error_payload.get("code") or error_payload.get("type")
                            error_message = error_payload.get("message") or error_message
                            success = False
                        data_payload = response_json.get("data") if isinstance(response_json, dict) else None
                        if isinstance(data_payload, dict):
                            notion_task_id = (
                                data_payload.get("notion_page_id")
                                or data_payload.get("notion_task_id")
                                or data_payload.get("page_id")
                            )
                        if isinstance(response_json, dict) and response_json.get("status") == "error":
                            success = False
                except Exception as exc:
                    error_message = str(exc)

                result = {
                    "action": action_name,
                    "endpoint": endpoint,
                    "request_id": request_envelope.get("request_id"),
                    "idempotency_key": request_envelope.get("idempotency_key"),
                    "status_code": status_code,
                    "success": success,
                    "response_body": response_body,
                    "response_json": response_json,
                    "error_code": error_code,
                    "error": error_message,
                    "notion_task_id": notion_task_id,
                }
                results.append(result)

                persist_artifact(
                    packet={
                        "request": request_envelope,
                        "response": {
                            "status_code": status_code,
                            "body": response_body,
                            "json": response_json,
                            "error": error_message,
                            "error_code": error_code,
                        },
                        "success": success,
                    },
                    kind="action",
                    intent_type=None,
                    action=action_name,
                    intent_id=intent_id,
                    correlation_id=correlation_id,
                    status="executed" if success else "failed",
                    idempotency_key=request_envelope.get("idempotency_key"),
                    settings=settings,
                )

        all_success = all(item["success"] for item in results)
        return all_success, results

    def build_plan(intent_id: str, correlation_id: str, final_canonical: Dict[str, Any]) -> Plan:
        intent_type = final_canonical.get("intent_type")
        fields = final_canonical.get("fields", {})
        if intent_type == "update_task":
            action_name = "notion.tasks.update"
            payload = {
                "notion_page_id": fields.get("task_id"),
                "patch": fields.get("patch", {}),
            }
        elif intent_type == "add_list_item":
            action_name = "notion.list.add_item"
            payload = fields
        elif intent_type == "capture_note":
            action_name = "notion.note.capture"
            payload = fields
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
        if intent_status in {"executed", "failed"}:
            outcome = load_outcome_response(intent_id)
            if outcome:
                return outcome
            return IngestResponse(
                status=intent_status,
                intent_id=intent_id,
                correlation_id=correlation_id,
                message="Intent completed" if intent_status == "executed" else "Intent failed",
            )
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
                error=build_error_payload(
                    "INTENT_FAILED",
                    "Intent rejected",
                    status_code=status.HTTP_400_BAD_REQUEST,
                ),
            )
        return IngestResponse(
            status="accepted",
            intent_id=intent_id,
            correlation_id=correlation_id,
            message="Intent accepted",
        )

    def attach_request_id(response_payload: IngestResponse, request_id: str) -> None:
        details = dict(response_payload.details or {})
        details.setdefault("request_id", request_id)
        response_payload.details = details

    def attach_receipt_fields(
        response_payload: IngestResponse,
        *,
        intent_id: str,
        trace_id: str,
        idempotency_key: str,
        overwrite: bool = True,
    ) -> None:
        if overwrite or response_payload.receipt_id is None:
            response_payload.receipt_id = intent_id
        if overwrite or response_payload.trace_id is None:
            response_payload.trace_id = trace_id
        if overwrite or response_payload.idempotency_key is None:
            response_payload.idempotency_key = idempotency_key

    def response_from_envelope(envelope: Dict[str, Any]) -> Optional[IngestResponse]:
        try:
            return IngestResponse.model_validate(envelope)
        except ValidationError:
            return None

    def load_outcome_response(intent_id: str) -> Optional[IngestResponse]:
        artifact = get_latest_intent_artifact(
            app.state.engine,
            intent_id=intent_id,
            kind="intent",
            status="executed",
        )
        if not artifact:
            artifact = get_latest_intent_artifact(
                app.state.engine,
                intent_id=intent_id,
                kind="intent",
                status="failed",
            )
        if not artifact:
            artifact = get_latest_intent_artifact(
                app.state.engine,
                intent_id=intent_id,
                kind="intent",
                status="rejected",
            )
        if not artifact:
            return None
        payload = artifact.get("artifact") or {}
        try:
            return IngestResponse.model_validate(payload)
        except Exception:
            return None

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
    async def ingest_intent(
        request: Request,
        response: Response,
        _: None = Depends(require_bearer),
        settings: Settings = Depends(get_settings),
        x_actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    ) -> IngestResponse:
        try:
            payload = json.loads(await request.body())
        except json.JSONDecodeError:
            return build_error_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="bad_json",
                message="Invalid JSON payload",
            )

        if not isinstance(payload, dict):
            return build_error_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="schema_validation_failed",
                message="Intent payload must be a JSON object",
            )

        schema_version = payload.get("schema_version")
        if schema_version and schema_version != "v1":
            return build_error_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="unsupported_schema_version",
                message=f"Unsupported schema_version: {schema_version}",
            )

        try:
            packet = IntentPacket.model_validate(payload)
        except ValidationError as exc:
            return build_error_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="schema_validation_failed",
                message="Intent payload failed schema validation",
                details={"errors": exc.errors()},
            )

        idempotency_key = compute_idempotency_key(payload)
        request_id, _ = compute_request_id(payload, idempotency_key)
        packet_data = packet.model_dump(mode="json", exclude_none=True)
        packet_data["request_id"] = request_id
        intent_id = packet.intent_id or new_intent_id()
        correlation_id = packet.correlation_id or new_correlation_id()
        trace_id = new_trace_id()
        actor_id = x_actor_id or packet_data.get("actor_id")
        packet_data["intent_id"] = intent_id
        packet_data["correlation_id"] = correlation_id
        packet_data["trace_id"] = trace_id
        if actor_id:
            packet_data["actor_id"] = actor_id

        try:
            intent_row, created = upsert_intent_by_idempotency_key(
                app.state.engine,
                intent_id=intent_id,
                idempotency_key=idempotency_key,
                status="received",
                raw_packet=payload,
                correlation_id=correlation_id,
                trace_id=trace_id,
                actor_id=actor_id,
            )
        except SQLAlchemyError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")

        intent_id = intent_row["intent_id"]
        correlation_id = intent_row["correlation_id"]
        trace_id = intent_row.get("trace_id") or trace_id
        packet_data["intent_id"] = intent_id
        packet_data["correlation_id"] = correlation_id
        packet_data["trace_id"] = trace_id

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
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Trace-Id"] = trace_id

        logger.info(
            "intent_ingest_received receipt_id=%s trace_id=%s idempotency_key=%s",
            intent_id,
            trace_id,
            idempotency_key,
        )

        if not created:
            stored_envelope = intent_row.get("response_envelope_json")
            if stored_envelope:
                stored_response = response_from_envelope(stored_envelope)
                if stored_response:
                    attach_receipt_fields(
                        stored_response,
                        intent_id=intent_id,
                        trace_id=trace_id,
                        idempotency_key=idempotency_key,
                        overwrite=False,
                    )
                    return stored_response
            outcome = load_outcome_response(intent_id)
            if outcome:
                attach_receipt_fields(
                    outcome,
                    intent_id=intent_id,
                    trace_id=trace_id,
                    idempotency_key=idempotency_key,
                )
                return outcome
            clarification_row = None
            if intent_row["status"] == "needs_clarification":
                clarification_row = get_open_clarification_for_intent(
                    app.state.engine,
                    intent_id,
                    actor_id=actor_id,
                    expiry_hours=settings.clarification_expiry_hours,
                )
            response_payload = outcome_response_from_intent(intent_row, clarification_row)
            attach_request_id(response_payload, request_id)
            attach_receipt_fields(
                response_payload,
                intent_id=intent_id,
                trace_id=trace_id,
                idempotency_key=idempotency_key,
            )
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
                update_intent(
                    app.state.engine,
                    intent_id=intent_id,
                    response_envelope_json=response_payload.model_dump(mode="json", exclude_none=True),
                )
            except SQLAlchemyError:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")
            logger.info(
                "intent_ingest_duplicate receipt_id=%s trace_id=%s idempotency_key=%s status=%s",
                intent_id,
                trace_id,
                idempotency_key,
                response_payload.status,
            )
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
            attach_request_id(response_payload, request_id)
            attach_receipt_fields(
                response_payload,
                intent_id=intent_id,
                trace_id=trace_id,
                idempotency_key=idempotency_key,
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
            update_intent(
                app.state.engine,
                intent_id=intent_id,
                response_envelope_json=response_payload.model_dump(mode="json", exclude_none=True),
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
            if settings.execute_actions:
                try:
                    all_success, execution_results = execute_plan(
                        intent_id=intent_id,
                        correlation_id=correlation_id,
                        actor_id=actor_id,
                        request_id=request_id,
                        plan=plan,
                        settings=settings,
                    )
                except ValueError as exc:
                    intent_row = update_intent(app.state.engine, intent_id=intent_id, status="failed")
                    response_payload = IngestResponse(
                        status="failed",
                        error_code="EXECUTION_NOT_CONFIGURED",
                        message=str(exc),
                        details={"execution_results": []},
                        intent_id=intent_id,
                        correlation_id=correlation_id,
                        error=build_error_payload(
                            "EXECUTION_NOT_CONFIGURED",
                            str(exc),
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        ),
                    )
                    attach_request_id(response_payload, request_id)
                    attach_receipt_fields(
                        response_payload,
                        intent_id=intent_id,
                        trace_id=trace_id,
                        idempotency_key=idempotency_key,
                    )
                    persist_artifact(
                        packet=response_payload.model_dump(mode="json", exclude_none=True),
                        kind="intent",
                        intent_type=packet.intent_type,
                        action=None,
                        intent_id=intent_id,
                        correlation_id=correlation_id,
                        status="failed",
                        idempotency_key=idempotency_key,
                        settings=settings,
                    )
                    update_intent(
                        app.state.engine,
                        intent_id=intent_id,
                        response_envelope_json=response_payload.model_dump(mode="json", exclude_none=True),
                    )
                    logger.info(
                        "intent_ingest_failed receipt_id=%s trace_id=%s idempotency_key=%s status=failed",
                        intent_id,
                        trace_id,
                        idempotency_key,
                    )
                    return response_payload

                if not all_success:
                    update_intent(app.state.engine, intent_id=intent_id, status="failed")
                    failure = next((item for item in execution_results if not item["success"]), None)
                    error_code = failure.get("error_code") if failure else None
                    if failure:
                        error_message = failure.get("error") or "One or more actions failed"
                        status_code = failure.get("status_code") or status.HTTP_502_BAD_GATEWAY
                    else:
                        error_message = "Execution failed"
                        status_code = status.HTTP_502_BAD_GATEWAY
                    error_details = {}
                    if failure:
                        error_details = {
                            "status_code": status_code,
                            "endpoint": failure.get("endpoint"),
                            "request_id": failure.get("request_id"),
                            "idempotency_key": failure.get("idempotency_key"),
                        }
                    response_payload = IngestResponse(
                        status="failed",
                        error_code="EXECUTION_FAILED",
                        message="One or more actions failed",
                        details={"execution_results": execution_results},
                        intent_id=intent_id,
                        correlation_id=correlation_id,
                        error=build_error_payload(
                            error_code or "EXECUTION_FAILED",
                            error_message,
                            status_code=status_code,
                            details=error_details,
                        ),
                    )
                    attach_request_id(response_payload, request_id)
                    attach_receipt_fields(
                        response_payload,
                        intent_id=intent_id,
                        trace_id=trace_id,
                        idempotency_key=idempotency_key,
                    )
                    persist_artifact(
                        packet=response_payload.model_dump(mode="json", exclude_none=True),
                        kind="intent",
                        intent_type=packet.intent_type,
                        action=None,
                        intent_id=intent_id,
                        correlation_id=correlation_id,
                        status="failed",
                        idempotency_key=idempotency_key,
                        settings=settings,
                    )
                    update_intent(
                        app.state.engine,
                        intent_id=intent_id,
                        response_envelope_json=response_payload.model_dump(mode="json", exclude_none=True),
                    )
                    logger.info(
                        "intent_ingest_failed receipt_id=%s trace_id=%s idempotency_key=%s status=failed",
                        intent_id,
                        trace_id,
                        idempotency_key,
                    )
                    return response_payload

                notion_task_id = None
                for item in execution_results:
                    if item.get("notion_task_id"):
                        notion_task_id = item["notion_task_id"]
                        break
                update_intent(app.state.engine, intent_id=intent_id, status="executed")
                response_payload.status = "executed"
                response_payload.details = {
                    "execution_results": execution_results,
                    "notion_task_id": notion_task_id,
                    "request_id": request_id,
                }
                attach_receipt_fields(
                    response_payload,
                    intent_id=intent_id,
                    trace_id=trace_id,
                    idempotency_key=idempotency_key,
                )
                persist_artifact(
                    packet=response_payload.model_dump(mode="json", exclude_none=True),
                    kind="intent",
                    intent_type=packet.intent_type,
                    action=None,
                    intent_id=intent_id,
                    correlation_id=correlation_id,
                    status="executed",
                    idempotency_key=idempotency_key,
                    settings=settings,
                )
                update_intent(
                    app.state.engine,
                    intent_id=intent_id,
                    response_envelope_json=response_payload.model_dump(mode="json", exclude_none=True),
                )
            attach_request_id(response_payload, request_id)
            attach_receipt_fields(
                response_payload,
                intent_id=intent_id,
                trace_id=trace_id,
                idempotency_key=idempotency_key,
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
            update_intent(
                app.state.engine,
                intent_id=intent_id,
                response_envelope_json=response_payload.model_dump(mode="json", exclude_none=True),
            )
            logger.info(
                "intent_ingest_ready receipt_id=%s trace_id=%s idempotency_key=%s status=%s",
                intent_id,
                trace_id,
                idempotency_key,
                response_payload.status,
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
            error=build_error_payload(
                result.error_code or "REJECTED",
                result.message or "Intent rejected",
                status_code=status.HTTP_400_BAD_REQUEST,
            ),
        )
        attach_request_id(response_payload, request_id)
        attach_receipt_fields(
            response_payload,
            intent_id=intent_id,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
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
        update_intent(
            app.state.engine,
            intent_id=intent_id,
            response_envelope_json=response_payload.model_dump(mode="json", exclude_none=True),
        )
        logger.info(
            "intent_ingest_rejected receipt_id=%s trace_id=%s idempotency_key=%s status=rejected",
            intent_id,
            trace_id,
            idempotency_key,
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
                error=build_error_payload(
                    "VALIDATION_ERROR",
                    "Missing action",
                    status_code=status.HTTP_400_BAD_REQUEST,
                ),
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
            error=build_error_payload(
                result.error_code or "REJECTED",
                result.message or "Intent rejected",
                status_code=status.HTTP_400_BAD_REQUEST,
            ),
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
