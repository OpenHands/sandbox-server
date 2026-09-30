"""Widen conversation_metadata token counters from INTEGER to BIGINT

The stats webhook callback writes accumulated token counts to the
``conversation_metadata`` table. These columns were created as ``INTEGER``
(PostgreSQL 32-bit, max ~2.1 billion). Long-running conversations can
accumulate token counts that exceed INT32, causing the DB write to fail and
the webhook to return a 500.

This migration widens every cumulative token counter to ``BIGINT`` (64-bit,
max ~9.2 quintillion). On PostgreSQL this is a safe in-place widening cast
with no data loss. ``batch_alter_table`` keeps it valid on SQLite (which
stores integers dynamically and needs a table rebuild for a type change).

Revision ID: 015
Revises: 014
Create Date: 2026-09-29 00:00:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '015'
down_revision: str | None = '014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Token counter columns on conversation_metadata that accumulate and can
# exceed INT32 across long conversations.
_TOKEN_COLUMNS = [
    'prompt_tokens',
    'completion_tokens',
    'total_tokens',
    'cache_read_tokens',
    'cache_write_tokens',
    'reasoning_tokens',
    'context_window',
    'per_turn_token',
]


def upgrade() -> None:
    with op.batch_alter_table('conversation_metadata') as batch_op:
        for column_name in _TOKEN_COLUMNS:
            batch_op.alter_column(
                column_name,
                existing_type=sa.Integer(),
                type_=sa.BigInteger(),
                existing_nullable=True,
            )


def downgrade() -> None:
    # Narrowing BIGINT -> INTEGER is only safe when no stored value exceeds
    # INT32; if any value does, the cast fails loudly rather than truncating.
    with op.batch_alter_table('conversation_metadata') as batch_op:
        for column_name in _TOKEN_COLUMNS:
            batch_op.alter_column(
                column_name,
                existing_type=sa.BigInteger(),
                type_=sa.Integer(),
                existing_nullable=True,
            )
