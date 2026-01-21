from __future__ import annotations

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
    canonical_draft: Optional[Dict[str, Any]] = None,
    final_canonical: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], bool]:
    stmt = (
        pg_insert(intents)
        .values(
            intent_id=intent_id,
            status=status,
            idempotency_key=idempotency_key,
            raw_packet=raw_packet,
            canonical_draft=canonical_draft,
            final_canonical=final_canonical,
            correlation_id=correlation_id,
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
        return dict(existing), False


def update_intent(
    engine: Engine,
    *,
    intent_id: str,
    status: Optional[str] = None,
    canonical_draft: Optional[Dict[str, Any]] = None,
    final_canonical: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
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


def get_open_clarification_for_intent(engine: Engine, intent_id: str) -> Optional[Dict[str, Any]]:
    with engine.begin() as conn:
        row = conn.execute(
            select(clarifications)
            .where(clarifications.c.intent_id == intent_id, clarifications.c.status == "open")
            .order_by(clarifications.c.created_at.desc())
        ).mappings().first()
        return dict(row) if row else None


def list_open_clarifications(engine: Engine) -> list[Dict[str, Any]]:
    with engine.begin() as conn:
        rows = conn.execute(
            select(clarifications)
            .where(clarifications.c.status == "open")
            .order_by(clarifications.c.created_at.asc())
        ).mappings().all()
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


def insert_intent_artifact(engine: Engine, payload: Dict[str, Any]) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(intent_artifacts.insert().values(**payload))
    except SQLAlchemyError as exc:
        raise exc
