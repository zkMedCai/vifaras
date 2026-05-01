"""audit_log: relax user_id nullability, add actor_ip, action+time index

Revision ID: a522942e0df5
Revises: a4c70b1aee1c
Create Date: 2026-05-01 15:24:24.128553

7.1.5 — record pre-auth security/abuse events that have no `user_id`
(rate-limit hit on /api/auth/*, sequential-register burst from one IP)
without a sentinel UUID. `actor_ip` becomes a first-class column so
analytics doesn't have to parse JSONB params for the "where" axis of
the who/what/when/where audit shape. The new `(action, timestamp)`
index supports the sequential-email detection query.

The autogenerate output for this revision included spurious diffs
against existing tables (HNSW index on intents, partial indexes on
matches, DESC ordering on notifications, several `server_default`
strips) that reflect model/DB representation drift, NOT intended
changes. Those have been removed; this migration is targeted to
audit_log only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a522942e0df5'
down_revision: Union[str, Sequence[str], None] = 'a4c70b1aee1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'audit_log',
        sa.Column('actor_ip', sa.String(length=45), nullable=True),
    )
    op.alter_column(
        'audit_log',
        'user_id',
        existing_type=sa.UUID(),
        nullable=True,
    )
    op.create_index(
        'ix_audit_action_time',
        'audit_log',
        ['action', 'timestamp'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    The relaxed `user_id` is repopulated as `NOT NULL` blindly. If any
    rows were inserted with NULL user_id between upgrade and downgrade
    (i.e. real anonymous events), this will fail — caller must clean
    those rows first. That's acceptable: downgrade is a recovery path,
    not a routine flow.
    """
    op.drop_index('ix_audit_action_time', table_name='audit_log')
    op.alter_column(
        'audit_log',
        'user_id',
        existing_type=sa.UUID(),
        nullable=False,
    )
    op.drop_column('audit_log', 'actor_ip')
