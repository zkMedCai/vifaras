"""add tier and relax nullifier_hash NOT NULL

Brief §2.5 (v1.1): tier-based onboarding posticipato.
  tier=0 → email + passkey only (no Self proof, no nullifier_hash)
  tier=1 → Self ZK proof verified (nullifier_hash populated)
  tier=2 → mandate signed (agent active)

This migration unblocks tier=0 storage:
  - ADD users.tier INTEGER NOT NULL DEFAULT 0 (server_default backfills the
    seed/dev users to tier=0 — semantically coherent: they pre-exist v1.1
    so they are at the lowest tier).
  - ALTER users.nullifier_hash DROP NOT NULL (so tier=0 rows can store NULL).

Note on uniqueness: ix_users_nullifier_hash is a Postgres unique INDEX (not a
table-level UNIQUE constraint). Postgres treats NULLs as distinct in unique
indexes by default (NULL ≠ NULL), so multiple tier=0 rows with NULL
nullifier_hash are allowed without further changes. No partial-unique index
needed. Verified against the initial migration 5ef3a914c6e6 + Postgres docs.

attributes_proven / attributes_verified_at / attributes_expires_at remain
NOT NULL by design — at tier=0 the auth_service writes placeholders ({},
NOW, NOW+1d) that are overwritten when 2.3 lands a real Self proof. See
DESIGN_QUESTIONS.md DQ-8.

Revision ID: e25338f5705c
Revises: 5ef3a914c6e6
Create Date: 2026-04-27 22:00:13.172333
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e25338f5705c"
down_revision: Union[str, Sequence[str], None] = "5ef3a914c6e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "tier",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.alter_column(
        "users",
        "nullifier_hash",
        existing_type=sa.TEXT(),
        nullable=True,
    )


def downgrade() -> None:
    # NB: if any tier=0 rows exist with nullifier_hash IS NULL, this downgrade
    # will fail. That's expected — recovery is operator-driven (purge tier=0
    # rows or backfill nullifier_hash) before downgrade.
    op.alter_column(
        "users",
        "nullifier_hash",
        existing_type=sa.TEXT(),
        nullable=False,
    )
    op.drop_column("users", "tier")
