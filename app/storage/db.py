from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.storage.schema import intent_artifacts


def create_db_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, future=True)


def check_db(engine: Engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def insert_intent_artifact(engine: Engine, payload: Dict[str, Any]) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(intent_artifacts.insert().values(**payload))
    except SQLAlchemyError as exc:
        raise exc
