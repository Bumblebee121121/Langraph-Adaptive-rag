"""Durable session memory and confirmation proposals.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_memory",
        sa.Column(
            "session_id", sa.String(64), sa.ForeignKey("sessions.session_id"), primary_key=True
        ),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column("summary_message_id", sa.Integer(), nullable=False),
        sa.Column("forgotten_message_id", sa.Integer(), nullable=False),
    )
    op.create_table(
        "pending_actions",
        sa.Column("action_id", sa.String(64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.session_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("action_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "message_metadata",
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("messages.id"), primary_key=True),
        sa.Column("response_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    for name in ("message_metadata", "pending_actions", "conversation_memory"):
        op.drop_table(name)
