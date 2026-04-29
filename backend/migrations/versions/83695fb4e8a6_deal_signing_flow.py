"""Deal signing flow: schema reconciliation + deal_signature_drafts table

Brief task 5.3 (Deal service). Reconciles the §5 scaffold's `Deal` /
`DealMessage` columns with the post-4.x marketplace shape, and adds the
`deal_signature_drafts` table that mirrors `mandate_drafts` (2.4) for the
buyer/seller WebAuthn signing flow.

Deal changes:
  - rename `final_price_cents` → `agreed_price_cents` (more accurate:
    the price has been *agreed* in the negotiation; "final" is reserved
    for V1.5+ post-Trustee terminal state). No live rows yet, safe rename.
  - add `currency CHAR(3)` defaulted to 'EUR' (V0 fixed; multi-currency V1+).
  - add `expires_at TIMESTAMPTZ NOT NULL` with server_default 24h ahead;
    application code overrides this on insert with the precise value.
  - add `cancelled_at TIMESTAMPTZ`, `cancellation_reason VARCHAR(50)`.
  - status default flipped from `pending_buyer` to `pending_signatures`
    — the new state machine treats both signatures symmetrically (no
    "buyer first then seller" ordering; either party can sign first).

DealMessage changes:
  - `encrypted_content` Text → BYTEA. The §5 scaffold stored as text
    (assumed base64). True E2E binary blobs are cleaner, and the FASE 11
    mobile client will produce raw bytes from libsodium.
  - add `nonce BYTEA NOT NULL` (server_default empty, application sets it).
  - rename `created_at` → `sent_at`. The chat domain has its own time-
    semantics ("when sent", not "when DB row was inserted").

deal_signature_drafts (new):
  Same shape as mandate_drafts: short-TTL row carrying the canonical
  bytes the user's passkey will sign + the WebAuthn challenge.
  Discriminated by `kind` (sign | cancel) so the same table backs both
  flows. `role` (buyer | seller) identifies which party is signing.

Revision ID: 83695fb4e8a6
Revises: e42f1c9ed0a1
Create Date: 2026-04-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "83695fb4e8a6"
down_revision: Union[str, Sequence[str], None] = "e42f1c9ed0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------- Deal -------------
    op.alter_column(
        "deals", "final_price_cents", new_column_name="agreed_price_cents"
    )
    op.add_column(
        "deals",
        sa.Column(
            "currency",
            sa.String(length=3),
            nullable=False,
            server_default="EUR",
        ),
    )
    op.add_column(
        "deals",
        sa.Column(
            "expires_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW() + interval '24 hours'"),
        ),
    )
    op.add_column(
        "deals",
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "deals",
        sa.Column("cancellation_reason", sa.String(length=50), nullable=True),
    )
    op.alter_column(
        "deals",
        "status",
        existing_type=sa.String(length=20),
        server_default="pending_signatures",
    )

    # ------------- DealMessage -------------
    # Text → BYTEA. No live rows, drop+add is the safe path.
    op.drop_column("deal_messages", "encrypted_content")
    op.add_column(
        "deal_messages",
        sa.Column("encrypted_content", sa.LargeBinary(), nullable=False),
    )
    op.add_column(
        "deal_messages",
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
    )
    op.alter_column(
        "deal_messages", "created_at", new_column_name="sent_at"
    )

    # ------------- deal_signature_drafts (new) -------------
    op.create_table(
        "deal_signature_drafts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
        ),
        sa.Column(
            "deal_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("deals.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        # buyer | seller — which side of the deal is signing
        sa.Column("role", sa.String(length=10), nullable=False),
        # sign | cancel — what the signature authorizes
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("canonical_payload", sa.LargeBinary(), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "consumed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_deal_drafts_deal_user",
        "deal_signature_drafts",
        ["deal_id", "user_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_deal_drafts_deal_user", table_name="deal_signature_drafts"
    )
    op.drop_table("deal_signature_drafts")

    op.alter_column(
        "deal_messages", "sent_at", new_column_name="created_at"
    )
    op.drop_column("deal_messages", "nonce")
    op.drop_column("deal_messages", "encrypted_content")
    op.add_column(
        "deal_messages",
        sa.Column("encrypted_content", sa.Text(), nullable=False),
    )

    op.alter_column(
        "deals",
        "status",
        existing_type=sa.String(length=20),
        server_default="pending_buyer",
    )
    op.drop_column("deals", "cancellation_reason")
    op.drop_column("deals", "cancelled_at")
    op.drop_column("deals", "expires_at")
    op.drop_column("deals", "currency")
    op.alter_column(
        "deals", "agreed_price_cents", new_column_name="final_price_cents"
    )
