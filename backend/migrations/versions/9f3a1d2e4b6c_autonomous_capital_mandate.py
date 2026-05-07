"""autonomous capital mandate

Revision ID: 9f3a1d2e4b6c
Revises: 6b4f2c91a0d2
Create Date: 2026-05-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9f3a1d2e4b6c"
down_revision: str | Sequence[str] | None = "6b4f2c91a0d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capital_mandates",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("agents.id"),
            nullable=False,
        ),
        sa.Column(
            "base_mandate_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("mandates.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("budget_total_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("max_single_purchase_cents", sa.BigInteger(), nullable=False),
        sa.Column("max_open_positions", sa.Integer(), nullable=False),
        sa.Column("max_daily_deals", sa.Integer(), nullable=True),
        sa.Column("min_expected_margin_bps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_total_loss_cents", sa.BigInteger(), nullable=True),
        sa.Column("risk_level", sa.String(length=10), nullable=False, server_default="medium"),
        sa.Column("auto_buy", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("auto_sell", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("auto_relist", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "requires_manual_approval",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "allowed_categories",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "forbidden_categories",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "geo_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "constraints",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("signature", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("canonical_payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("paused_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_capital_mandates_user_status",
        "capital_mandates",
        ["user_id", "status"],
    )
    op.create_index(
        "ix_capital_mandates_agent_status",
        "capital_mandates",
        ["agent_id", "status"],
    )

    op.create_table(
        "capital_mandate_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("agents.id"),
            nullable=False,
        ),
        sa.Column(
            "base_mandate_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("mandates.id"),
            nullable=False,
        ),
        sa.Column("canonical_payload", sa.LargeBinary(), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "ix_capital_mandate_drafts_user_expires",
        "capital_mandate_drafts",
        ["user_id", "expires_at"],
    )

    op.add_column(
        "deals",
        sa.Column("buyer_authorization_method", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "deals",
        sa.Column("seller_authorization_method", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "deals",
        sa.Column("buyer_capital_mandate_id", postgresql.UUID(as_uuid=False), nullable=True),
    )
    op.add_column(
        "deals",
        sa.Column("seller_capital_mandate_id", postgresql.UUID(as_uuid=False), nullable=True),
    )
    op.add_column("deals", sa.Column("buyer_authorized_at", sa.DateTime(), nullable=True))
    op.add_column("deals", sa.Column("seller_authorized_at", sa.DateTime(), nullable=True))
    op.create_foreign_key(
        "fk_deals_buyer_capital_mandate_id",
        "deals",
        "capital_mandates",
        ["buyer_capital_mandate_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_deals_seller_capital_mandate_id",
        "deals",
        "capital_mandates",
        ["seller_capital_mandate_id"],
        ["id"],
    )

    op.create_table(
        "capital_positions",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "capital_mandate_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("capital_mandates.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("agents.id"),
            nullable=False,
        ),
        sa.Column(
            "source_buy_deal_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("deals.id"),
            nullable=True,
        ),
        sa.Column(
            "resale_sell_deal_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("deals.id"),
            nullable=True,
        ),
        sa.Column(
            "item_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(length=40),
            nullable=False,
            server_default="opportunity_found",
        ),
        sa.Column("purchase_price_cents", sa.BigInteger(), nullable=True),
        sa.Column("expected_resale_price_cents", sa.BigInteger(), nullable=True),
        sa.Column("expected_profit_cents", sa.BigInteger(), nullable=True),
        sa.Column("expected_margin_bps", sa.Integer(), nullable=True),
        sa.Column("realized_sale_price_cents", sa.BigInteger(), nullable=True),
        sa.Column("realized_profit_cents", sa.BigInteger(), nullable=True),
        sa.Column("risk_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_capital_positions_mandate_status",
        "capital_positions",
        ["capital_mandate_id", "status"],
    )
    op.create_index(
        "ix_capital_positions_user_status",
        "capital_positions",
        ["user_id", "status"],
    )

    op.create_table(
        "capital_ledger_entries",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "capital_mandate_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("capital_mandates.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("agents.id"),
            nullable=False,
        ),
        sa.Column(
            "deal_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("deals.id"),
            nullable=True,
        ),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("capital_positions.id"),
            nullable=True,
        ),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "ix_capital_ledger_mandate_created",
        "capital_ledger_entries",
        ["capital_mandate_id", "created_at"],
    )
    op.create_index(
        "ix_capital_ledger_user_created",
        "capital_ledger_entries",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_capital_ledger_user_created", table_name="capital_ledger_entries")
    op.drop_index("ix_capital_ledger_mandate_created", table_name="capital_ledger_entries")
    op.drop_table("capital_ledger_entries")

    op.drop_index("ix_capital_positions_user_status", table_name="capital_positions")
    op.drop_index("ix_capital_positions_mandate_status", table_name="capital_positions")
    op.drop_table("capital_positions")

    op.drop_constraint("fk_deals_seller_capital_mandate_id", "deals", type_="foreignkey")
    op.drop_constraint("fk_deals_buyer_capital_mandate_id", "deals", type_="foreignkey")
    op.drop_column("deals", "seller_authorized_at")
    op.drop_column("deals", "buyer_authorized_at")
    op.drop_column("deals", "seller_capital_mandate_id")
    op.drop_column("deals", "buyer_capital_mandate_id")
    op.drop_column("deals", "seller_authorization_method")
    op.drop_column("deals", "buyer_authorization_method")

    op.drop_index(
        "ix_capital_mandate_drafts_user_expires",
        table_name="capital_mandate_drafts",
    )
    op.drop_table("capital_mandate_drafts")

    op.drop_index("ix_capital_mandates_agent_status", table_name="capital_mandates")
    op.drop_index("ix_capital_mandates_user_status", table_name="capital_mandates")
    op.drop_table("capital_mandates")
