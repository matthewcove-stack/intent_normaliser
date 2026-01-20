from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func, text

metadata = MetaData()

intent_artifacts = Table(
    "intent_artifacts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("intent_id", Text, nullable=False, unique=True),
    Column("correlation_id", Text, nullable=False),
    Column("supersedes_intent_id", Text, nullable=True),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("kind", Text, nullable=False),
    Column("intent_type", Text, nullable=True),
    Column("action", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("idempotency_key", Text, nullable=True),
    Column("artifact_version", Integer, nullable=False, server_default=text("1")),
    Column("artifact_hash", Text, nullable=False),
    Column("artifact", JSONB, nullable=False),
    Index("ix_intent_artifacts_received_at", "received_at"),
    Index("ix_intent_artifacts_status", "status"),
    Index("ix_intent_artifacts_intent_type", "intent_type"),
    Index("ix_intent_artifacts_action", "action"),
    Index("ix_intent_artifacts_idempotency_key", "idempotency_key"),
    Index("ix_intent_artifacts_artifact", "artifact", postgresql_using="gin"),
)
