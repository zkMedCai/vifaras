"""HNSW index on intents.description_embedding + Match score breakdown

Brief task 4.3 (match service). Two related changes shipped together
because the matcher's persistence shape and its query plan are coupled.

Vector index (intents.description_embedding):
  HNSW with `vector_cosine_ops` — cosine distance is the only operator
  the matcher uses, and embeddings are unit-norm (true for both OpenAI
  text-embedding-3-small and the deterministic fake), so cosine is the
  semantically correct distance. Parameters: `m=16, ef_construction=64`,
  pgvector's standard sweet spot for the 1K–1M-vector regime V0 will
  see. `ef_search` (recall/speed knob) is left at the runtime default
  (40); tune it via `SET hnsw.ef_search = N` per session if recall
  becomes a problem at scale (V1+).

Match score breakdown:
  `similarity_score` already exists (cosine similarity, semantic-only).
  4.3 introduces `price_proximity_score` (price-axis match quality) and
  `combined_score` (the value the matcher actually ranks on,
  `0.7*similarity + 0.3*price_proximity` in V0). Storing all three
  separately means: (a) the API can surface the breakdown for
  transparency, (b) we can re-tune the weights against historical match
  data without re-computing similarity from scratch.

Composite filtered indexes:
  `(buy_intent_id, status, combined_score DESC) WHERE status='discovered'`
  and the `sell_intent_id` symmetric variant. The matcher's hot path is
  "give me the top-N discovered matches for this intent ranked by
  combined_score" — these indexes make that an index-only scan. Filtered
  on `status='discovered'` because terminal statuses (negotiating /
  agreed / rejected / expired) are read for history, not ranking.

Revision ID: e42f1c9ed0a1
Revises: 8df1d6891fd9
Create Date: 2026-04-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e42f1c9ed0a1"
down_revision: Union[str, Sequence[str], None] = "8df1d6891fd9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. HNSW index on description_embedding for vector cosine search.
    op.execute(
        "CREATE INDEX ix_intents_embedding_hnsw "
        "ON intents USING hnsw (description_embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # 2. Score breakdown columns on matches.
    op.add_column(
        "matches",
        sa.Column("price_proximity_score", sa.Numeric(5, 4), nullable=True),
    )
    op.add_column(
        "matches",
        sa.Column("combined_score", sa.Numeric(5, 4), nullable=True),
    )

    # 3. Composite filtered indexes for the matcher's hot ranking query.
    op.execute(
        "CREATE INDEX ix_matches_buy_intent_discovered_score "
        "ON matches (buy_intent_id, combined_score DESC) "
        "WHERE status = 'discovered'"
    )
    op.execute(
        "CREATE INDEX ix_matches_sell_intent_discovered_score "
        "ON matches (sell_intent_id, combined_score DESC) "
        "WHERE status = 'discovered'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_matches_sell_intent_discovered_score")
    op.execute("DROP INDEX IF EXISTS ix_matches_buy_intent_discovered_score")
    op.drop_column("matches", "combined_score")
    op.drop_column("matches", "price_proximity_score")
    op.execute("DROP INDEX IF EXISTS ix_intents_embedding_hnsw")
