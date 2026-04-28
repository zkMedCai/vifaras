"""add mandate_drafts table

Pending mandate drafts (brief task 2.4). A draft is created in
`POST /api/mandates/draft`, lives for 5 minutes, and is consumed once by
`POST /api/mandates/submit`. Replay is prevented by the `consumed` flag;
expired drafts are rejected at read time.

`canonical_payload` is the JCS-canonicalized (RFC 8785) JSON bytes that
the user's WebAuthn passkey signs. `challenge` is the random 32 bytes
that doubles as the WebAuthn challenge (also embedded inside the payload
under `payload.challenge` so the signed blob is bound to this draft).

Index on `(user_id, expires_at)` supports fast cleanup of expired drafts
and tier-2 idempotency lookups.

Revision ID: 5765c48f21ea
Revises: e25338f5705c
Create Date: 2026-04-28 20:14:30.686811
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5765c48f21ea"
down_revision: Union[str, Sequence[str], None] = "e25338f5705c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "mandate_drafts",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("agent_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("canonical_payload", sa.LargeBinary(), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "consumed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mandate_drafts_user_expires",
        "mandate_drafts",
        ["user_id", "expires_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_mandate_drafts_user_expires", table_name="mandate_drafts")
    op.drop_table("mandate_drafts")
