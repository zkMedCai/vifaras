"""deal trade window state

Revision ID: 2d0c7f8e9a31
Revises: 99d1cbef5405
Create Date: 2026-05-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2d0c7f8e9a31"
down_revision: str | Sequence[str] | None = "99d1cbef5405"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deals",
        sa.Column(
            "shipping_status",
            sa.String(length=30),
            nullable=False,
            server_default="shipping_pending",
        ),
    )
    op.add_column(
        "deals", sa.Column("tracking_reference", sa.Text(), nullable=True)
    )
    op.add_column("deals", sa.Column("shipped_at", sa.DateTime(), nullable=True))
    op.add_column(
        "deals", sa.Column("delivered_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "deals", sa.Column("completed_at", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("deals", "completed_at")
    op.drop_column("deals", "delivered_at")
    op.drop_column("deals", "shipped_at")
    op.drop_column("deals", "tracking_reference")
    op.drop_column("deals", "shipping_status")
