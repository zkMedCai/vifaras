"""agents.last_tick_at + last_tick_summary for the agent runtime

Brief task 6.2 (Agent state & inbox). The orchestrator (6.3) ticks
agents on a 60s scheduler; each tick reloads the full state via
`agent_state_service.get_full_state(agent_id)`. To bound the inbox
view to "what changed since last tick", we need to remember when the
last tick happened — hence `agents.last_tick_at`.

`last_tick_summary` is a debugging hook: each tick can stash a small
JSONB blob ("decided to send_offer at €120 on negotiation X") that's
useful for V0 founder-led inspection without standing up a full
observability stack. Bounded by orchestrator (small payload, <1 KB).

Both nullable: pre-6.2 agents have neither. The first tick after 6.2
deploy will insert them.

Revision ID: a718c85956d0
Revises: 8325e74a8074
Create Date: 2026-04-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a718c85956d0"
down_revision: Union[str, Sequence[str], None] = "8325e74a8074"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("last_tick_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column(
            "last_tick_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "last_tick_summary")
    op.drop_column("agents", "last_tick_at")
