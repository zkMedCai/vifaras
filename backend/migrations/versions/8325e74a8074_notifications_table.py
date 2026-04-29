"""notifications table for the user-facing UX layer

Brief task 6.1 (Notification service). The new table holds per-user
notifications: step-up requests, match discoveries, negotiation events,
deal lifecycle, chat messages received. The 4.x/5.x services emit them
*post-commit, fire-and-forget* — failure to persist a notification must
never roll back the underlying business action (see `notification_service`
docstring).

Schema:
  - `type` is the fine-grained event identifier (e.g. `offer_received`).
  - `category` is the coarse bucket the UI uses for tabs/icons
    (`step_up | match | negotiation | deal | agent`). Stored separately
    from `type` so a `WHERE category='deal'` filter is index-friendly
    and we don't have to maintain a Python-side mapping in queries.
  - `payload` JSONB carries the structured data the client UI uses for
    deep-linking (e.g. `{deal_id: '...'}` or `{negotiation_id: ..., turn_number: ...}`).
  - `read_at` / `acted_at` distinguish "user saw it" from "user took
    the suggested action" (e.g. opened a step-up vs. signed it).
  - `expires_at` is policy-driven by emitter: step-ups ~10 min, deals
    ~24h, matches ~30d. Cleanup is a scheduler job.

Indexes:
  - `ix_notifications_user_unread`: filtered partial index on
    `read_at IS NULL`. The "badge with unread count" query is the
    hot path (every UI mount); a partial index keeps it cheap.
  - `ix_notifications_user_recent`: covers the general list query
    (newest-first, paginated).

Revision ID: 8325e74a8074
Revises: 83695fb4e8a6
Create Date: 2026-04-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "8325e74a8074"
down_revision: Union[str, Sequence[str], None] = "83695fb4e8a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("acted_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    # Partial index for the "unread" hot path.
    op.execute(
        "CREATE INDEX ix_notifications_user_unread "
        "ON notifications (user_id, created_at DESC) "
        "WHERE read_at IS NULL"
    )
    op.create_index(
        "ix_notifications_user_recent",
        "notifications",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notifications_user_recent", table_name="notifications"
    )
    op.execute("DROP INDEX IF EXISTS ix_notifications_user_unread")
    op.drop_table("notifications")
