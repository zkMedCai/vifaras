"""add mandate_revocation_drafts and step_up_requests tables

Brief task 2.5. Two new tables:

- `mandate_revocation_drafts` — pending revocation drafts (mirror of
  `mandate_drafts` but bound to the specific mandate being revoked).
  WebAuthn-signed flow: /draft creates a row with the canonical bytes
  and challenge, /submit verifies the signature and revokes the mandate.
- `step_up_requests` — paused agent actions awaiting user confirmation.
  Created by tool_layer when MandateVerifier raises StepUpRequired;
  signed by the user via /api/step-up/{id}/sign; consumed when the
  agent re-attempts the action with the captured signature.

Revision ID: 52b8a8ddb144
Revises: 5765c48f21ea
Create Date: 2026-04-28 21:01:31.495198
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "52b8a8ddb144"
down_revision: Union[str, Sequence[str], None] = "5765c48f21ea"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "mandate_revocation_drafts",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("mandate_id", sa.UUID(as_uuid=False), nullable=False),
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
        sa.ForeignKeyConstraint(["mandate_id"], ["mandates.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_revocation_drafts_user_expires",
        "mandate_revocation_drafts",
        ["user_id", "expires_at"],
    )

    op.create_table(
        "step_up_requests",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("agent_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("mandate_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column(
            "action_params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("canonical_payload", sa.LargeBinary(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column(
            "signature", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["mandate_id"], ["mandates.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_step_up_pending_user",
        "step_up_requests",
        ["user_id", "status"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_step_up_pending_user",
        table_name="step_up_requests",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("step_up_requests")
    op.drop_index(
        "ix_revocation_drafts_user_expires",
        table_name="mandate_revocation_drafts",
    )
    op.drop_table("mandate_revocation_drafts")
