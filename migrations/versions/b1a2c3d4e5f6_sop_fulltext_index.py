"""Full-text search index for SOP documents

The Staff Assistant agent answers questions over the SOP corpus. A GIN index on
a tsvector of title+body gives good-enough retrieval for a corpus of this size
without introducing a vector store as an external dependency.

Revision ID: b1a2c3d4e5f6
Revises: 47fe5f700800
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b1a2c3d4e5f6"
down_revision: str | None = "47fe5f700800"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX ix_sop_document_fts ON sop_document
        USING GIN (to_tsvector('english', title || ' ' || body))
        """
    )
    # Trigram index on ingredient names, used to fuzzy-match free-text supplier
    # invoice line descriptions back to stock items.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX ix_ingredient_name_trgm ON ingredient USING GIN (name gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ingredient_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_sop_document_fts")
