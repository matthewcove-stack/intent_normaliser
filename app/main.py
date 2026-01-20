from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, settings as default_settings
from app.models.packets import ActionPacket, IngestResponse, IntentPacket
from app.storage.db import check_db, create_db_engine, insert_intent_artifact
from app.util.canonical import canonical_json
from app.util.hashing import sha256_hex
from app.util.ids import new_correlation_id, new_intent_id


NOT_IMPLEMENTED_MESSAGE = "Phase 0: normalisation and execution are not implemented"


def create_app(app_settings: Settings | None = None) -> FastAPI:
    app_settings = app_settings or default_settings
    app = FastAPI()

    app.state.settings = app_settings
    app.state.engine = create_db_engine(app_settings.database_url)

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

    def persist_received(
        packet: Dict[str, Any],
        kind: str,
        intent_type: str | None,
        action: str | None,
        intent_id: str,
        correlation_id: str,
        settings: Settings,
    ) -> None:
        received_at = datetime.now(timezone.utc)
        artifact = {
            "packet": packet,
            "intent_id": intent_id,
            "correlation_id": correlation_id,
            "server_received_at": received_at.isoformat(),
        }
        artifact_payload = {
            "intent_id": intent_id,
            "correlation_id": correlation_id,
            "supersedes_intent_id": None,
            "kind": kind,
            "intent_type": intent_type,
            "action": action,
            "status": "received",
            "idempotency_key": None,
            "artifact_version": settings.artifact_version,
            "artifact_hash": sha256_hex(canonical_json(artifact)),
            "artifact": artifact,
        }
        insert_intent_artifact(app.state.engine, artifact_payload)

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

    @app.post("/v1/ingest/intent", response_model=IngestResponse)
    def ingest_intent(
        packet: IntentPacket,
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
            persist_received(
                packet=packet_data,
                kind="intent",
                intent_type=packet.intent_type,
                action=None,
                intent_id=intent_id,
                correlation_id=correlation_id,
                settings=settings,
            )
        except SQLAlchemyError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")

        response.headers["X-Intent-Id"] = intent_id
        response.headers["X-Correlation-Id"] = correlation_id
        return IngestResponse(
            status="rejected",
            error_code="NOT_IMPLEMENTED",
            message=NOT_IMPLEMENTED_MESSAGE,
            intent_id=intent_id,
            correlation_id=correlation_id,
        )

    @app.post("/v1/ingest/action", response_model=IngestResponse)
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
            persist_received(
                packet=packet_data,
                kind="action",
                intent_type=None,
                action=packet.action,
                intent_id=intent_id,
                correlation_id=correlation_id,
                settings=settings,
            )
        except SQLAlchemyError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")

        response.headers["X-Intent-Id"] = intent_id
        response.headers["X-Correlation-Id"] = correlation_id
        return IngestResponse(
            status="rejected",
            error_code="NOT_IMPLEMENTED",
            message=NOT_IMPLEMENTED_MESSAGE,
            intent_id=intent_id,
            correlation_id=correlation_id,
        )

    return app


app = create_app()
