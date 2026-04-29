"""user_questions table for the agent's `ask_user` tool

Brief task 6.3.a (modernize tool_layer). The `ask_user` tool lets an
agent surface a question to its principal when it can't decide
unilaterally (e.g. floor unreachable mid-negotiation, ambiguous user
preference). V0 implementation is a stub: we persist the question +
emit an `AGENT_QUESTION` notification. The user answers via the mobile
app (FASE 11); the agent picks up the answer at the next tick via
`read_inbox`.

Schema:
  - `agent_id` / `user_id`: which agent asked, which user it's for.
  - `question` / `context`: free text + structured context blob.
  - `status`: `pending` (default) | `answered` | `expired`.
  - `answer`: free text once provided.
  - `expires_at`: V0 default 24h; if no answer by then, auto-expire.

Indexes: (agent_id, status) for the agent's "do I have pending Qs?"
read; (user_id, status) for the UI "questions awaiting your answer".

Revision ID: 3e6079aa6977
Revises: a718c85956d0
Create Date: 2026-04-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "3e6079aa6977"
down_revision: Union[str, Sequence[str], None] = "a718c85956d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_questions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("agents.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answered_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_user_questions_agent_status",
        "user_questions",
        ["agent_id", "status"],
    )
    op.create_index(
        "ix_user_questions_user_status",
        "user_questions",
        ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_questions_user_status", table_name="user_questions"
    )
    op.drop_index(
        "ix_user_questions_agent_status", table_name="user_questions"
    )
    op.drop_table("user_questions")
