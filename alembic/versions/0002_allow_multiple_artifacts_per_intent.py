from __future__ import annotations

from alembic import op

revision = "0002_allow_multiple_artifacts_per_intent"
down_revision = "0001_create_intent_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("intent_artifacts_intent_id_key", "intent_artifacts", type_="unique")
    op.create_index("ix_intent_artifacts_intent_id", "intent_artifacts", ["intent_id"])


def downgrade() -> None:
    op.drop_index("ix_intent_artifacts_intent_id", table_name="intent_artifacts")
    op.create_unique_constraint("intent_artifacts_intent_id_key", "intent_artifacts", ["intent_id"])
