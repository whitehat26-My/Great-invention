"""Reminders — the owner's own work, held somewhere

A restaurant that has traded twenty years runs on things nobody wrote down: the
halal certificate, the extinguisher service, the answer the landlord wants by
Friday. They are remembered until the week they are not, and a lapsed licence
closes the door.

Revision ID: d3c4e5f6a7b8
Revises: c2b3d4e5f6a7
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3c4e5f6a7b8"
down_revision: str | None = "c2b3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reminder",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("what", sa.String(length=300), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("raised_by", sa.String(length=60), nullable=False, server_default="owner"),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_raised_on", sa.Date(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_reminder_due_on", "reminder", ["due_on"])
    # The question asked constantly is "what is open and due", never "what is done".
    op.create_index("ix_reminder_open", "reminder", ["due_on", "done_at"])


def downgrade() -> None:
    op.drop_index("ix_reminder_open", table_name="reminder")
    op.drop_index("ix_reminder_due_on", table_name="reminder")
    op.drop_table("reminder")
