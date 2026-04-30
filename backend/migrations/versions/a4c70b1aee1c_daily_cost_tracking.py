"""daily_cost_tracking table for orchestrator cost cap (brief task 6.3.c)

The agent scheduler (6.3.c) enforces a daily LLM cost cap to bound
worst-case spend if the orchestrator misbehaves or an adversarial
pattern emerges. The cap is a soft kill-switch: when crossed, the
scheduler stops dispatching ticks for the rest of the UTC day.

Implementation strategy: a single row per UTC date holding the
cumulative cost + tick count. Each tick UPSERTs (`INSERT ... ON
CONFLICT DO UPDATE`) so we never need a separate "init today's row"
job. The PK is the date itself, so the table grows by one row per
day — at 365 rows/year it's negligible storage.

Why a dedicated table instead of summing over `audit_log` per
discovery cycle: cost-per-tick is structured numeric data, audit_log
stores it inside a JSONB params blob. A targeted UPSERT here is a
single index hit; the alternative is a JSONB extraction + SUM over
potentially thousands of rows, run every minute. Tradeoff: tiny extra
write per tick for a fast read on the cap check.

V1+ extensions: per-agent / per-user cost tracking will likely live
in a sibling table once we surface user-facing cost limits. This
table stays as the global aggregate.

Revision ID: a4c70b1aee1c
Revises: 3e6079aa6977
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4c70b1aee1c"
down_revision: Union[str, Sequence[str], None] = "3e6079aa6977"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_table("daily_cost_tracking")
