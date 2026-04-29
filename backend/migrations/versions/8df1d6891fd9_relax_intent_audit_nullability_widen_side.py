"""relax intent + audit nullability for tier-0 actions; widen intents.side

Brief task 4.1 (FASE 4 — marketplace core): tier-0 users (no agent yet)
can create intents. The current schema has three NOT NULL columns that
implicitly assume "every intent / audit row was produced by an agent
under an active mandate" — true pre-v1.1 onboarding, no longer true now
that intent CRUD is exposed at tier 0.

Changes:
  - `intents.agent_id` NOT NULL → NULL. Tier-0 intents have no agent
    binding; the matching service still works (queries by user_id and
    semantic similarity, not agent identity). Cascade revoke in
    `mandate_revocation_service` continues to use `Intent.agent_id ==
    agent_id`, which Postgres correctly evaluates as false for NULLs —
    so tier-0 intents are immune to revocation cascades by design.
  - `audit_log.agent_id` NOT NULL → NULL.
  - `audit_log.mandate_id` NOT NULL → NULL.
    These two unify the audit channel: marketplace actions taken before
    a mandate exists (intent create at tier 0, intent update at tier 1)
    can now write `AuditLog` rows with NULL mandate/agent. The structlog
    channel in `audit_service.py` stays for identity-lifecycle events
    (tier upgrade, mandate signed) that semantically don't fit a
    per-action audit row even with relaxed FKs.

  - `intents.side` String(4) → String(5). Schema-ready for `'trade'`
    (PROJECT_BRIEF §2.9). V0 service-layer rejects 'trade' before any
    DB write, but keeping the column too narrow to even hold the value
    is a future foot-gun — widening costs nothing.

Indexes / unique constraints / FKs are unaffected. Postgres treats NULLs
as distinct in unique indexes by default, so multiple NULL agent_ids on
audit_log raise no constraint conflicts.

Revision ID: 8df1d6891fd9
Revises: 52b8a8ddb144
Create Date: 2026-04-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8df1d6891fd9"
down_revision: Union[str, Sequence[str], None] = "52b8a8ddb144"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "intents",
        "agent_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=False),
        nullable=True,
    )
    op.alter_column(
        "intents",
        "side",
        existing_type=sa.String(length=4),
        type_=sa.String(length=5),
        existing_nullable=False,
    )
    op.alter_column(
        "audit_log",
        "agent_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=False),
        nullable=True,
    )
    op.alter_column(
        "audit_log",
        "mandate_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=False),
        nullable=True,
    )


def downgrade() -> None:
    # Downgrade fails if any row has the corresponding column NULL — that's
    # expected. Recovery is operator-driven (purge tier-0 intents / NULL
    # audit rows) before downgrading.
    op.alter_column(
        "audit_log",
        "mandate_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=False),
        nullable=False,
    )
    op.alter_column(
        "audit_log",
        "agent_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=False),
        nullable=False,
    )
    op.alter_column(
        "intents",
        "side",
        existing_type=sa.String(length=5),
        type_=sa.String(length=4),
        existing_nullable=False,
    )
    op.alter_column(
        "intents",
        "agent_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=False),
        nullable=False,
    )
