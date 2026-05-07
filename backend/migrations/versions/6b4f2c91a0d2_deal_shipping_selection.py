"""deal shipping selection

Revision ID: 6b4f2c91a0d2
Revises: 2d0c7f8e9a31
Create Date: 2026-05-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "6b4f2c91a0d2"
down_revision: str | Sequence[str] | None = "2d0c7f8e9a31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deal_shipping_selections",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "deal_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("deals.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("method_code", sa.String(length=50), nullable=False),
        sa.Column("method_label", sa.Text(), nullable=False),
        sa.Column("method_description", sa.Text(), nullable=False),
        sa.Column("price_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            nullable=False,
            server_default="EUR",
        ),
        sa.Column("paid_by", sa.String(length=10), nullable=False),
        sa.Column("tracking_required", sa.Boolean(), nullable=False),
        sa.Column("insurance_available", sa.Boolean(), nullable=False),
        sa.Column("insurance_required", sa.Boolean(), nullable=False),
        sa.Column(
            "recommended",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("risk_level", sa.String(length=10), nullable=False),
        sa.Column(
            "selected_by_user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("selected_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column(
            "policy_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_table("deal_shipping_selections")
