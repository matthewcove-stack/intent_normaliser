from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_add_intents_clarifications"
down_revision = "0002_allow_multi_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intents",
        sa.Column("intent_id", sa.Text(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("raw_packet", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("canonical_draft", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("final_canonical", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
    )

    op.create_index("ix_intents_status", "intents", ["status"])
    op.create_index("ix_intents_idempotency_key", "intents", ["idempotency_key"])

    op.create_table(
        "clarifications",
        sa.Column("clarification_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("intent_id", sa.Text(), sa.ForeignKey("intents.intent_id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expected_answer_type", sa.Text(), nullable=False),
        sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("answer", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_clarifications_status", "clarifications", ["status"])
    op.create_index("ix_clarifications_intent_id", "clarifications", ["intent_id"])


def downgrade() -> None:
    op.drop_index("ix_clarifications_intent_id", table_name="clarifications")
    op.drop_index("ix_clarifications_status", table_name="clarifications")
    op.drop_table("clarifications")
    op.drop_index("ix_intents_idempotency_key", table_name="intents")
    op.drop_index("ix_intents_status", table_name="intents")
    op.drop_table("intents")
