from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, Optional, Tuple

from sqlalchemy import Engine, create_engine, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.storage.schema import clarifications, intent_artifacts, intents


def create_db_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, future=True)


def check_db(engine: Engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def upsert_intent_by_idempotency_key(
    engine: Engine,
    *,
    intent_id: str,
    idempotency_key: str,
    status: str,
    raw_packet: Dict[str, Any],
    correlation_id: str,
    trace_id: str,
    actor_id: Optional[str] = None,
    canonical_draft: Optional[Dict[str, Any]] = None,
    final_canonical: Optional[Dict[str, Any]] = None,
    response_envelope_json: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], bool]:
    stmt = (
        pg_insert(intents)
        .values(
            intent_id=intent_id,
            status=status,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            raw_packet=raw_packet,
            canonical_draft=canonical_draft,
            final_canonical=final_canonical,
            correlation_id=correlation_id,
            trace_id=trace_id,
            response_envelope_json=response_envelope_json,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(*intents.c)
    )
    with engine.begin() as conn:
        inserted = conn.execute(stmt).mappings().first()
        if inserted:
            return dict(inserted), True
        existing = conn.execute(
            select(intents).where(intents.c.idempotency_key == idempotency_key)
        ).mappings().first()
        if not existing:
            raise SQLAlchemyError("Idempotency upsert failed to return a row")
        existing_dict = dict(existing)
        if actor_id and not existing_dict.get("actor_id"):
            existing_dict = update_intent(engine, intent_id=existing_dict["intent_id"], actor_id=actor_id)
        if trace_id and not existing_dict.get("trace_id"):
            existing_dict = update_intent(engine, intent_id=existing_dict["intent_id"], trace_id=trace_id)
        return existing_dict, False


def update_intent(
    engine: Engine,
    *,
    intent_id: str,
    status: Optional[str] = None,
    canonical_draft: Optional[Dict[str, Any]] = None,
    final_canonical: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    response_envelope_json: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    values: Dict[str, Any] = {"updated_at": text("now()")}
    if status is not None:
        values["status"] = status
    if canonical_draft is not None:
        values["canonical_draft"] = canonical_draft
    if final_canonical is not None:
        values["final_canonical"] = final_canonical
    if correlation_id is not None:
        values["correlation_id"] = correlation_id
    if actor_id is not None:
        values["actor_id"] = actor_id
    if trace_id is not None:
        values["trace_id"] = trace_id
    if response_envelope_json is not None:
        values["response_envelope_json"] = response_envelope_json
    stmt = (
        update(intents)
        .where(intents.c.intent_id == intent_id)
        .values(**values)
        .returning(*intents.c)
    )
    with engine.begin() as conn:
        row = conn.execute(stmt).mappings().first()
        if not row:
            raise SQLAlchemyError("Intent not found for update")
        return dict(row)


def get_intent(engine: Engine, intent_id: str) -> Optional[Dict[str, Any]]:
    with engine.begin() as conn:
        row = conn.execute(select(intents).where(intents.c.intent_id == intent_id)).mappings().first()
        return dict(row) if row else None


def create_clarification(
    engine: Engine,
    *,
    intent_id: str,
    status: str,
    question: str,
    expected_answer_type: str,
    candidates: Iterable[Dict[str, Any]],
    answer: Optional[Dict[str, Any]] = None,
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    stmt = (
        clarifications.insert()
        .values(
            intent_id=intent_id,
            status=status,
            question=question,
            expected_answer_type=expected_answer_type,
            candidates=list(candidates),
            answer=answer,
            actor_id=actor_id,
        )
        .returning(*clarifications.c)
    )
    with engine.begin() as conn:
        row = conn.execute(stmt).mappings().first()
        if not row:
            raise SQLAlchemyError("Clarification insert failed")
        return dict(row)


def get_clarification(engine: Engine, clarification_id: str) -> Optional[Dict[str, Any]]:
    with engine.begin() as conn:
        row = conn.execute(
            select(clarifications).where(clarifications.c.clarification_id == clarification_id)
        ).mappings().first()
        return dict(row) if row else None


def get_open_clarification_for_intent(
    engine: Engine,
    intent_id: str,
    *,
    actor_id: Optional[str] = None,
    expiry_hours: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if expiry_hours is not None:
        expire_open_clarifications(engine, expiry_hours, intent_id=intent_id, actor_id=actor_id)
    stmt = select(clarifications).where(
        clarifications.c.intent_id == intent_id,
        clarifications.c.status == "open",
    )
    if actor_id:
        stmt = stmt.where(clarifications.c.actor_id == actor_id)
    stmt = stmt.order_by(clarifications.c.created_at.desc())
    with engine.begin() as conn:
        row = conn.execute(stmt).mappings().first()
        return dict(row) if row else None


def list_open_clarifications(
    engine: Engine,
    *,
    actor_id: Optional[str] = None,
    expiry_hours: Optional[int] = None,
) -> list[Dict[str, Any]]:
    if expiry_hours is not None:
        expire_open_clarifications(engine, expiry_hours, actor_id=actor_id)
    stmt = select(clarifications).where(clarifications.c.status == "open")
    if actor_id:
        stmt = stmt.where(clarifications.c.actor_id == actor_id)
    stmt = stmt.order_by(clarifications.c.created_at.asc())
    with engine.begin() as conn:
        rows = conn.execute(stmt).mappings().all()
        return [dict(row) for row in rows]


def answer_clarification(
    engine: Engine,
    *,
    clarification_id: str,
    answer_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    stmt = (
        update(clarifications)
        .where(
            clarifications.c.clarification_id == clarification_id,
            clarifications.c.status == "open",
        )
        .values(
            status="answered",
            answer=answer_payload,
            answered_at=text("now()"),
        )
        .returning(*clarifications.c)
    )
    with engine.begin() as conn:
        row = conn.execute(stmt).mappings().first()
        return dict(row) if row else None


def expire_open_clarifications(
    engine: Engine,
    expiry_hours: int,
    *,
    intent_id: Optional[str] = None,
    actor_id: Optional[str] = None,
) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=expiry_hours)
    stmt = update(clarifications).where(
        clarifications.c.status == "open",
        clarifications.c.created_at < cutoff,
    )
    if intent_id:
        stmt = stmt.where(clarifications.c.intent_id == intent_id)
    if actor_id:
        stmt = stmt.where(clarifications.c.actor_id == actor_id)
    stmt = stmt.values(status="expired").returning(clarifications.c.intent_id)
    with engine.begin() as conn:
        intent_rows = conn.execute(stmt).fetchall()
        intent_ids = [row[0] for row in intent_rows if row[0]]
        if intent_ids:
            conn.execute(
                update(intents)
                .where(intents.c.intent_id.in_(intent_ids), intents.c.status == "needs_clarification")
                .values(status="expired", updated_at=text("now()"))
            )
        return intent_ids


def expire_clarification(
    engine: Engine,
    *,
    clarification_id: str,
) -> Optional[Dict[str, Any]]:
    stmt = (
        update(clarifications)
        .where(
            clarifications.c.clarification_id == clarification_id,
            clarifications.c.status == "open",
        )
        .values(status="expired")
        .returning(*clarifications.c)
    )
    with engine.begin() as conn:
        row = conn.execute(stmt).mappings().first()
        return dict(row) if row else None


def insert_intent_artifact(engine: Engine, payload: Dict[str, Any]) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(intent_artifacts.insert().values(**payload))
    except SQLAlchemyError as exc:
        raise exc


def get_latest_intent_artifact(
    engine: Engine,
    *,
    intent_id: str,
    kind: Optional[str] = None,
    status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    stmt = select(intent_artifacts).where(intent_artifacts.c.intent_id == intent_id)
    if kind is not None:
        stmt = stmt.where(intent_artifacts.c.kind == kind)
    if status is not None:
        stmt = stmt.where(intent_artifacts.c.status == status)
    stmt = stmt.order_by(intent_artifacts.c.received_at.desc())
    with engine.begin() as conn:
        row = conn.execute(stmt).mappings().first()
        return dict(row) if row else None
