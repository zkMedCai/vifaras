"""daily_cost_tracking per-user (brief task 7.3.2)

Composite primary key on (date, user_id). Soft cap per-user (settable
via `daily_user_cost_cap_usd`) requires per-user accumulation; the
global hard cap (`max_daily_llm_cost_usd`) is preserved by summing
across users for any given UTC date.

V0 dev environment: the table currently has no production-meaningful
rows (kill-switch never triggered, alpha tester data only). Drop +
recreate is the cleanest path — preserves zero rows we'd want to keep
and avoids the gymnastics of backfilling a NOT NULL column behind a
sentinel user_id.

Migration written by hand, NOT alembic autogenerate. Reason: schema
drift between `app.models.schema` and the live DB (HNSW index, partial
indexes on `matches`, DESC ordering on notifications, server_defaults)
means autogenerate produces a noisy diff that would clobber unrelated
schema. Pattern preserved from [7.1.5]: filter manually, apply only
the target diff.

Index on (user_id, date): the dominant query is "today's spend for
user X" (soft cap check, dispatched on every scheduler discovery
cycle for every candidate user). The composite PK starts with date
because the global SUM-cross-users ranges by date; the supplementary
index reverses the order for the per-user lookup.

Revision ID: b7c1e2f3a4d5
Revises: a522942e0df5
Create Date: 2026-05-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7c1e2f3a4d5"
down_revision: Union[str, Sequence[str], None] = "a522942e0df5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # V0 dev: drop and recreate. NO production data to preserve.
    op.drop_table("daily_cost_tracking")
    op.create_table(
        "daily_cost_tracking",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "total_cost_usd",
            sa.Numeric(12, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "tick_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("date", "user_id"),
    )
    op.create_index(
        "ix_daily_cost_user_date",
        "daily_cost_tracking",
        ["user_id", "date"],
    )


def downgrade() -> None:
    # NOTE: lossy downgrade — does NOT restore the (date)-only PK
    # structure of revision a4c70b1aee1c. V0 dev mindset: rollback to
    # this point requires manual schema restoration. Acceptable
    # because there is no production data path that exercises it.
    op.drop_index("ix_daily_cost_user_date", table_name="daily_cost_tracking")
    op.drop_table("daily_cost_tracking")
    op.create_table(
        "daily_cost_tracking",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column(
            "total_cost_usd",
            sa.Numeric(12, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "tick_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
