"""Repair intermediate conversation memory schemas missing the forgetting cursor.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Canonical 0003 already has this field; early installations may not.
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("conversation_memory")
    }
    if "forgotten_message_id" not in columns:
        op.add_column(
            "conversation_memory",
            sa.Column("forgotten_message_id", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    # Keep the field: it belongs to canonical 0003 as well. Removing it would
    # resurrect forgotten history or recreate the startup failure.
    pass
