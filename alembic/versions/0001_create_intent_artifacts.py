from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_create_intent_artifacts"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "intent_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("intent_id", sa.Text(), nullable=False, unique=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("supersedes_intent_id", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("intent_type", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("artifact_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("artifact_hash", sa.Text(), nullable=False),
        sa.Column("artifact", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )

    op.create_index("ix_intent_artifacts_received_at", "intent_artifacts", ["received_at"])
    op.create_index("ix_intent_artifacts_status", "intent_artifacts", ["status"])
    op.create_index("ix_intent_artifacts_intent_type", "intent_artifacts", ["intent_type"])
    op.create_index("ix_intent_artifacts_action", "intent_artifacts", ["action"])
    op.create_index("ix_intent_artifacts_idempotency_key", "intent_artifacts", ["idempotency_key"])
    op.create_index(
        "ix_intent_artifacts_artifact",
        "intent_artifacts",
        ["artifact"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_intent_artifacts_artifact", table_name="intent_artifacts")
    op.drop_index("ix_intent_artifacts_idempotency_key", table_name="intent_artifacts")
    op.drop_index("ix_intent_artifacts_action", table_name="intent_artifacts")
    op.drop_index("ix_intent_artifacts_intent_type", table_name="intent_artifacts")
    op.drop_index("ix_intent_artifacts_status", table_name="intent_artifacts")
    op.drop_index("ix_intent_artifacts_received_at", table_name="intent_artifacts")
    op.drop_table("intent_artifacts")
