from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_add_actor_id_intents"
down_revision = "0003_add_intents_clarifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("intents", sa.Column("actor_id", sa.Text(), nullable=True))
    op.create_index("ix_intents_actor_id", "intents", ["actor_id"])

    op.add_column("clarifications", sa.Column("actor_id", sa.Text(), nullable=True))
    op.create_index("ix_clarifications_actor_id", "clarifications", ["actor_id"])


def downgrade() -> None:
    op.drop_index("ix_clarifications_actor_id", table_name="clarifications")
    op.drop_column("clarifications", "actor_id")

    op.drop_index("ix_intents_actor_id", table_name="intents")
    op.drop_column("intents", "actor_id")
