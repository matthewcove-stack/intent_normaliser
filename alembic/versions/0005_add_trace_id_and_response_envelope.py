from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_trace_id_envelope"
down_revision = "0004_add_actor_id_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "intents",
        sa.Column(
            "trace_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()::text"),
        ),
    )
    op.add_column("intents", sa.Column("response_envelope_json", sa.dialects.postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("intents", "response_envelope_json")
    op.drop_column("intents", "trace_id")
