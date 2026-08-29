"""Conversation memory for the approvals chat

Every message was answered alone, so the question desk could not follow a
conversation: "how much chicken is left?" worked and "and rice?" meant nothing.

It is a table rather than memory in the listener because the listener is
restarted — by the supervisor when a child dies, by a reboot, by an upgrade —
and a thread that survives the question but not the restart is worse than no
thread at all, because it is unpredictable.

Revision ID: c2b3d4e5f6a7
Revises: b1a2c3d4e5f6
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2b3d4e5f6a7"
down_revision: str | None = "b1a2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_turn",
        # String(36), matching UUIDPk — the ids are generated application-side.
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("chat_id", sa.String(length=40), nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("said_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversation_turn_chat_id", "conversation_turn", ["chat_id"])
    op.create_index("ix_conversation_turn_said_at", "conversation_turn", ["said_at"])
    # The read is always "this chat, most recent first", so it gets its own.
    op.create_index("ix_conversation_chat_time", "conversation_turn", ["chat_id", "said_at"])


def downgrade() -> None:
    op.drop_index("ix_conversation_chat_time", table_name="conversation_turn")
    op.drop_index("ix_conversation_turn_said_at", table_name="conversation_turn")
    op.drop_index("ix_conversation_turn_chat_id", table_name="conversation_turn")
    op.drop_table("conversation_turn")
