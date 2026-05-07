"""
Schema database del marketplace.

Principio guida: ZERO PII memorizzata.
Identità = nullifier opaco da Self Protocol.
"""
from datetime import datetime, timedelta
from enum import Enum
from sqlalchemy import (
    Column, String, Integer, Numeric, Date, DateTime, Boolean,
    ForeignKey, Text, JSON, BigInteger, Index, UniqueConstraint, LargeBinary,
    func, text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base, relationship
from pgvector.sqlalchemy import Vector
import uuid

Base = declarative_base()


def gen_uuid():
    return str(uuid.uuid4())


# ============================================================================
# IDENTITY LAYER
# ============================================================================

class User(Base):
    """
    L'utente è solo un nullifier + passkey.
    Niente nome, niente email obbligatoria, niente CF.

    v1.1 (brief §2.5): tier-based onboarding posticipato.
      tier=0 anonymous (email + passkey),
      tier=1 identified (Self ZK proof verified),
      tier=2 mandated (mandate signed, agent active).
    nullifier_hash è nullable a tier=0, popolato a tier=1.
    """
    __tablename__ = "users"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)

    # Tier corrente di onboarding. Monotonicamente crescente: 0 → 1 → 2.
    tier = Column(Integer, nullable=False, default=0, server_default=text("0"))

    # Identità ZK (popolato a tier ≥ 1)
    nullifier_hash = Column(Text, unique=True, nullable=True, index=True)

    # Attributi dimostrati via Self (selective disclosure)
    # Solo flag, mai dati personali. Es: {"adult": true, "country": "IT", "valid": true}
    # A tier=0 viene popolato con placeholder ({}, NOW, NOW+1d). Sovrascritto a tier=1.
    attributes_proven = Column(JSONB, nullable=False)
    attributes_verified_at = Column(DateTime, nullable=False)
    attributes_expires_at = Column(DateTime, nullable=False)  # = scadenza documento
    
    # Auth
    passkey_credential_id = Column(Text, nullable=False)
    passkey_pubkey = Column(Text, nullable=False)
    passkey_sign_count = Column(Integer, default=0)
    
    # Email opzionale per notifiche, mai usata come identificatore
    notification_email = Column(Text, nullable=True)
    push_token = Column(Text, nullable=True)
    
    # Stato
    status = Column(String(20), default="active")  # active|banned|inactive
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_active_at = Column(DateTime, default=datetime.utcnow)
    
    # Relazioni
    agents = relationship("Agent", back_populates="user")
    intents = relationship("Intent", back_populates="user")
    mandates = relationship("Mandate", back_populates="user")


class Agent(Base):
    """Un agente è un keypair + un mandate attivo."""
    __tablename__ = "agents"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    
    name = Column(String(100))  # nickname dato dall'utente, es. "Compra GPU"
    pubkey = Column(Text, nullable=False)
    privkey_kms_ref = Column(Text, nullable=False)  # ref nel KMS, NON la chiave
    
    status = Column(String(20), default="active")  # active|paused|revoked
    created_at = Column(DateTime, default=datetime.utcnow)

    # 6.2: orchestrator tick tracking. `last_tick_at` is the cursor for
    # "what's new since last visit" inbox queries. `last_tick_summary` is
    # a small JSONB debug blob (decided action, reason) — V0 founder-led
    # inspection lever before full observability lands in 7.x.
    last_tick_at = Column(DateTime, nullable=True)
    last_tick_summary = Column(JSONB, nullable=True)

    user = relationship("User", back_populates="agents")
    mandates = relationship("Mandate", back_populates="agent")


class Mandate(Base):
    """
    Il mandate è il contratto firmato dall'utente all'agente.
    Cuore del sistema: ogni azione viene verificata contro un mandate attivo.
    """
    __tablename__ = "mandates"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    agent_id = Column(UUID(as_uuid=False), ForeignKey("agents.id"), nullable=False)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    
    version = Column(String(10), default="1.0")
    
    # Scope: cosa l'agente può/non può fare (whitelist)
    # Vedi MANDATE_SPEC.md per il contratto JSON completo
    scope = Column(JSONB, nullable=False)
    limits = Column(JSONB, nullable=False)
    step_up_required_for = Column(JSONB, nullable=False)
    constraints = Column(JSONB, nullable=False)
    
    # Tracking utilizzo (per enforcement limiti)
    spent_total_eur = Column(Numeric(10, 2), default=0)
    deals_count = Column(Integer, default=0)
    spent_today_eur = Column(Numeric(10, 2), default=0)
    last_reset_date = Column(DateTime, default=datetime.utcnow)
    
    # Ciclo di vita
    issued_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    revocation_reason = Column(Text, nullable=True)
    
    # Firma WebAuthn dell'utente
    signature = Column(JSONB, nullable=False)
    canonical_payload = Column(Text, nullable=False)  # JSON serializzato canonicamente che è stato firmato
    
    user = relationship("User", back_populates="mandates")
    agent = relationship("Agent", back_populates="mandates")


class MandateDraft(Base):
    """
    Pending mandate draft awaiting WebAuthn signature.

    Created in /api/mandates/draft, consumed in /api/mandates/submit.
    Short TTL (5 min) — forces the user to complete the flow promptly.
    The `canonical_payload` bytes here are the EXACT bytes that will be
    signed by the user's passkey; submit re-canonicalization MUST yield
    the same bytes or the signature won't verify.

    `consumed=True` is the replay guard: a draft can be redeemed once.
    """
    __tablename__ = "mandate_drafts"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    agent_id = Column(UUID(as_uuid=False), ForeignKey("agents.id"), nullable=False)

    # The canonical (RFC 8785 / JCS) JSON bytes of the mandate payload.
    # WebAuthn challenge is `sha256(canonical_payload)`, so any byte-level
    # divergence between draft-time and submit-time would invalidate the
    # signature.
    canonical_payload = Column(LargeBinary, nullable=False)

    # Random 32 bytes generated server-side; doubles as the WebAuthn
    # challenge for the assertion. Echoed inside the payload itself
    # (payload.challenge field) so the signed blob proves binding to
    # this specific draft.
    challenge = Column(LargeBinary, nullable=False)

    expires_at = Column(DateTime, nullable=False)
    consumed = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_mandate_drafts_user_expires", "user_id", "expires_at"),
    )


class MandateRevocationDraft(Base):
    """
    Pending revocation draft awaiting WebAuthn signature (brief task 2.5).

    Same shape as MandateDraft but binds to the specific mandate being
    revoked via `mandate_id` FK. Carries the canonical bytes (action:
    revoke_mandate) + a 32-byte challenge that doubles as WebAuthn challenge.
    `consumed=True` is the replay guard.
    """
    __tablename__ = "mandate_revocation_drafts"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    mandate_id = Column(UUID(as_uuid=False), ForeignKey("mandates.id"), nullable=False)

    canonical_payload = Column(LargeBinary, nullable=False)
    challenge = Column(LargeBinary, nullable=False)

    expires_at = Column(DateTime, nullable=False)
    consumed = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index(
            "ix_revocation_drafts_user_expires", "user_id", "expires_at"
        ),
    )


class StepUpRequest(Base):
    """
    Step-up request: a paused agent action awaiting biometric confirmation.

    Created by `tool_layer.ToolHandler` when `MandateVerifier.authorize`
    raises `StepUpRequired` (action exceeds a step-up rule). The user
    sees it via `GET /api/step-up/pending`, signs it via
    `POST /api/step-up/{id}/sign`, or rejects via `/reject`. After
    approval, the agent re-attempts the original action with the
    captured signature attached (V0 sync resume).

    Status transitions:
        pending → approved   (user signed)
        pending → rejected   (user explicitly rejected)
        pending → expired    (TTL elapsed, ~10 min)
    Once non-pending, the row is read-only history.
    """
    __tablename__ = "step_up_requests"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    agent_id = Column(UUID(as_uuid=False), ForeignKey("agents.id"), nullable=False)
    mandate_id = Column(UUID(as_uuid=False), ForeignKey("mandates.id"), nullable=False)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)

    # The blocked action: agent re-tries it with this name + params (+ signature)
    # once the user approves.
    action = Column(String(50), nullable=False)
    action_params = Column(JSONB, nullable=False)
    reason = Column(Text, nullable=False)  # human-readable, e.g. "Price €120 above threshold €100"

    # Crypto seam — exact same pattern as MandateDraft.
    challenge = Column(LargeBinary, nullable=False)
    canonical_payload = Column(LargeBinary, nullable=False)

    status = Column(
        String(16),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    expires_at = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime, nullable=True)

    # Populated when status='approved': the verified WebAuthn assertion
    # the agent attaches to the resumed tool call.
    signature = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index(
            "ix_step_up_pending_user", "user_id", "status",
            postgresql_where=(status == "pending"),
        ),
    )


# ============================================================================
# MARKETPLACE LAYER
# ============================================================================

class Intent(Base):
    """
    BUY o SELL intent. È l'unità base del marketplace.
    Niente listing/buy_request separati: tutto è intent.
    """
    __tablename__ = "intents"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    # agent_id is nullable: tier-0 users have no agent yet but can create intents.
    # Cascade revoke (mandate_revocation_service) filters by agent_id; NULLs are
    # correctly skipped, so tier-0 intents are immune to revocation cascades.
    agent_id = Column(UUID(as_uuid=False), ForeignKey("agents.id"), nullable=True)

    # side is a 3-value enum at the spec level (PROJECT_BRIEF §2.9): 'buy' |
    # 'sell' | 'trade'. V0 service-layer rejects 'trade' before any DB write,
    # so only 'buy'/'sell' actually land — column width tolerates 'trade' for
    # forward-compat (FASE 8).
    side = Column(String(5), nullable=False)
    
    # Descrizione dell'oggetto
    title = Column(String(200), nullable=False)
    description = Column(Text)
    category = Column(String(50), nullable=False)
    description_embedding = Column(Vector(1536))  # per matching semantico
    
    # Prezzi (in centesimi per evitare float issues)
    reservation_price_cents = Column(BigInteger, nullable=False)  # floor sell, cap buy
    ideal_price_cents = Column(BigInteger, nullable=False)
    currency = Column(String(3), default="EUR")
    
    # Vincoli
    hard_constraints = Column(JSONB, default=dict)  # es {"location": "Roma", "delivery": "pickup"}
    soft_preferences = Column(JSONB, default=dict)
    
    # Stato
    status = Column(String(20), default="active")  # active|matched|closed|expired|cancelled
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)
    
    user = relationship("User", back_populates="intents")
    
    __table_args__ = (
        Index("ix_intents_active_category_side", "status", "category", "side"),
        # HNSW vector index for fast cosine-similarity search on intent
        # embeddings. Used by FASE 4.3 match pipeline (k-nearest neighbor
        # retrieval). PostgreSQL-specific syntax via the pgvector extension;
        # not portable to other DBs. m / ef_construction tuned at v0 default.
        Index(
            "ix_intents_embedding_hnsw",
            "description_embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"description_embedding": "vector_cosine_ops"},
        ),
    )


class Match(Base):
    """Match potenziale tra un buy_intent e un sell_intent."""
    __tablename__ = "matches"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    buy_intent_id = Column(UUID(as_uuid=False), ForeignKey("intents.id"), nullable=False)
    sell_intent_id = Column(UUID(as_uuid=False), ForeignKey("intents.id"), nullable=False)
    
    similarity_score = Column(Numeric(5, 4))  # cosine similarity, 0.0000-1.0000
    price_overlap = Column(Boolean, default=False)

    # 4.3: score breakdown. similarity_score above is semantic only; the
    # matcher ranks on `combined_score = 0.7*similarity + 0.3*price_proximity`.
    # Both nullable so legacy rows pre-4.3 don't break (none in V0 yet, but
    # cheap insurance). New rows always populate them.
    price_proximity_score = Column(Numeric(5, 4))
    combined_score = Column(Numeric(5, 4))

    status = Column(String(20), default="discovered")  # discovered|negotiating|agreed|rejected|expired
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("buy_intent_id", "sell_intent_id", name="uq_match"),
        # Partial indexes optimising "discovered" status queries — by far the
        # most common access pattern (scheduler scan, dashboard listings).
        # Filter clause keeps the index small and queries fast: only
        # candidate matches awaiting negotiation are in scope.
        Index(
            "ix_matches_buy_intent_discovered_score",
            "buy_intent_id",
            text("combined_score DESC"),
            postgresql_where=text("status = 'discovered'"),
        ),
        Index(
            "ix_matches_sell_intent_discovered_score",
            "sell_intent_id",
            text("combined_score DESC"),
            postgresql_where=text("status = 'discovered'"),
        ),
    )


class Negotiation(Base):
    """
    Una negoziazione tra due agenti su un match.
    State contiene la cronologia di offerte/contro-offerte.
    """
    __tablename__ = "negotiations"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    match_id = Column(UUID(as_uuid=False), ForeignKey("matches.id"), nullable=False)
    
    # Cronologia offerte (append-only)
    # [{round: 1, from: agent_id, type: 'offer'|'counter'|'accept'|'reject', price_cents: ..., message: ..., timestamp: ...}]
    state = Column(JSONB, default=list)
    
    rounds_used = Column(Integer, default=0)
    max_rounds = Column(Integer, default=6)  # EC6 hard cap
    
    current_price_cents = Column(BigInteger)
    
    status = Column(String(20), default="active")  # active|agreed|rejected|expired|capped
    started_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)


class Deal(Base):
    """
    Un deal confermato. Richiede step-up signature da entrambe le parti.

    State machine (post-5.3):
      pending_signatures → confirmed (both signed)
      pending_signatures → cancelled (explicit cancel by either party)
      pending_signatures → expired   (24h timeout without dual sign)
      confirmed          → completed (V1.5+ Trustee Service)
      confirmed          → disputed  (V1.5+ Trustee Service)
    """
    __tablename__ = "deals"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    negotiation_id = Column(UUID(as_uuid=False), ForeignKey("negotiations.id"), nullable=False)

    buyer_user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    seller_user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    buy_intent_id = Column(UUID(as_uuid=False), ForeignKey("intents.id"), nullable=False)
    sell_intent_id = Column(UUID(as_uuid=False), ForeignKey("intents.id"), nullable=False)

    # Renamed from `final_price_cents` in 5.3: "agreed" reflects the
    # negotiated value at accept; "final" is reserved for V1.5+ Trustee
    # post-settlement state.
    agreed_price_cents = Column(BigInteger, nullable=False)
    currency = Column(
        String(3), nullable=False, default="EUR", server_default=text("'EUR'")
    )

    # Step-up signatures (passkey-firmate dagli umani)
    buyer_signature = Column(JSONB, nullable=True)
    buyer_signed_at = Column(DateTime, nullable=True)
    seller_signature = Column(JSONB, nullable=True)
    seller_signed_at = Column(DateTime, nullable=True)

    status = Column(
        String(20),
        default="pending_signatures",
        server_default=text("'pending_signatures'"),
    )
    # pending_signatures | confirmed | cancelled | expired | completed | disputed

    created_at = Column(DateTime, default=datetime.utcnow)
    # Python-side default mirrors the migration's server_default (NOW()+24h).
    # deal_service overrides this on insert; the default here is a safety net
    # for legacy callers (test_revocation seed rows) and ORM consistency.
    expires_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.utcnow() + timedelta(hours=24),
        server_default=text("now() + interval '24 hours'"),
    )
    confirmed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancellation_reason = Column(String(50), nullable=True)

    # V0 Trade Window logistics state. Detailed escrow/tracking tables land
    # in Trustee Service V1.5; these columns keep the current flow durable.
    shipping_status = Column(
        String(30),
        nullable=False,
        default="shipping_pending",
        server_default=text("'shipping_pending'"),
    )
    tracking_reference = Column(Text, nullable=True)
    shipped_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # EC5: idempotency key per evitare double-deal su stessa negoziazione
    idempotency_key = Column(Text, unique=True, nullable=False)


class DealShippingSelection(Base):
    """Structured shipping method selected inside the Trade Window.

    `Deal.shipping_status` remains the single source of truth for the
    operational lifecycle. This table stores the selected method and the
    deterministic V0 policy snapshot used when the user selected it.
    """

    __tablename__ = "deal_shipping_selections"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    deal_id = Column(UUID(as_uuid=False), ForeignKey("deals.id"), nullable=False, unique=True)

    method_code = Column(String(50), nullable=False)
    method_label = Column(Text, nullable=False)
    method_description = Column(Text, nullable=False)
    price_cents = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="EUR", server_default=text("'EUR'"))
    paid_by = Column(String(10), nullable=False)

    tracking_required = Column(Boolean, nullable=False)
    insurance_available = Column(Boolean, nullable=False)
    insurance_required = Column(Boolean, nullable=False)
    recommended = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    risk_level = Column(String(10), nullable=False)

    selected_by_user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    selected_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=func.now())
    updated_at = Column(DateTime, nullable=True)
    policy_snapshot = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))


class DealSignatureDraft(Base):
    """Pending WebAuthn-bound signature draft for a Deal action (5.3).

    Same pattern as MandateDraft / MandateRevocationDraft (2.4 / 2.5):
    short-TTL row carrying the canonical bytes the user's passkey will
    sign + the WebAuthn challenge. `consumed=True` is the replay guard.

    Discriminated by `kind`:
      - 'sign'   → buyer or seller signing the deal to confirm.
      - 'cancel' → buyer or seller signing a deal cancellation.

    `role` identifies which side of the deal is signing.
    """
    __tablename__ = "deal_signature_drafts"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    deal_id = Column(UUID(as_uuid=False), ForeignKey("deals.id"), nullable=False)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)

    role = Column(String(10), nullable=False)  # 'buyer' | 'seller'
    kind = Column(String(10), nullable=False)  # 'sign'  | 'cancel'

    canonical_payload = Column(LargeBinary, nullable=False)
    challenge = Column(LargeBinary, nullable=False)

    expires_at = Column(DateTime, nullable=False)
    consumed = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at = Column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_deal_drafts_deal_user", "deal_id", "user_id", "expires_at"
        ),
    )


class DealMessage(Base):
    """Chat tra umani post-deal per coordinare consegna/pagamento. Pseudonimi.

    V0 backend is transport-only: `encrypted_content` and `nonce` are
    opaque binary blobs the server never decrypts. Real key exchange +
    encryption is FASE 11 (mobile client).
    """
    __tablename__ = "deal_messages"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    deal_id = Column(UUID(as_uuid=False), ForeignKey("deals.id"), nullable=False)
    sender_user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)

    encrypted_content = Column(LargeBinary, nullable=False)
    nonce = Column(LargeBinary, nullable=False)
    # nullable=True intentionally: ORM `default=datetime.utcnow` always fills
    # value on INSERT, so an explicit `sent_at=None` is the only path to a
    # NULL row. Strict `nullable=False` was a model-side regression vs the
    # migration history (see 5ef3a914c6e6_initial_schema), relaxed in [7.4.0]
    # reconciliation to align with DB. If a future requirement demands strict
    # NOT NULL, add a dedicated migration + revert this declaration.
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=True)


# ============================================================================
# AUDIT LAYER
# ============================================================================

class UserQuestion(Base):
    """Agent → user open question (brief task 6.3.a `ask_user` tool).

    V0 stub: persisted + notified, but the answering UX (mobile app) lands
    in FASE 11. The agent surfaces a free-text question with optional
    structured context; the user answers later, and the agent picks up
    the answer on the next tick via `read_inbox`.
    """
    __tablename__ = "user_questions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    agent_id = Column(UUID(as_uuid=False), ForeignKey("agents.id"), nullable=False)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)

    question = Column(Text, nullable=False)
    context = Column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    status = Column(
        String(20),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    # pending | answered | expired

    answer = Column(Text, nullable=True)
    answered_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_user_questions_agent_status", "agent_id", "status"),
        Index("ix_user_questions_user_status", "user_id", "status"),
    )


class Notification(Base):
    """Per-user UX-layer notification (brief task 6.1).

    Emitted post-commit by 4.x/5.x services as fire-and-forget. Failure
    to persist a row must never roll back the underlying business action.
    See `notification_service.create_notification`.
    """
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)

    type = Column(String(50), nullable=False)
    category = Column(String(20), nullable=False)

    title = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    payload = Column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    read_at = Column(DateTime, nullable=True)
    acted_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # `created_at DESC` ordering matches the dominant access pattern
        # (latest-first inbox listings + unread filter). Partial WHERE on
        # the unread variant keeps that index small.
        Index(
            "ix_notifications_user_unread",
            "user_id",
            text("created_at DESC"),
            postgresql_where=(read_at.is_(None)),
        ),
        Index(
            "ix_notifications_user_recent",
            "user_id",
            text("created_at DESC"),
        ),
    )


class AuditLog(Base):
    """
    Log immutabile di ogni azione agente.
    Naturalmente pseudonimo (user_id punta a nullifier).
    AI Act compliant by design.

    7.1.5 — `user_id` relaxed to nullable so pre-auth security events
    (rate limit hit on auth endpoints, sequential register-attempt
    burst from one IP) can be recorded without a sentinel UUID hack.
    `actor_ip` added as a first-class column so analytics (`who/what/
    when/where`) doesn't require parsing JSONB. Existing rows keep
    `user_id NOT NULL`-shape data — the relaxed constraint only
    accepts new NULL writes.
    """
    __tablename__ = "audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    user_id = Column(UUID(as_uuid=False), nullable=True)  # no FK per immutabilità; nullable post-7.1.5 for anonymous events
    # agent_id / mandate_id nullable: marketplace actions at tier 0 (intent CRUD
    # before mandate exists) write audit rows with NULL agent + NULL mandate.
    # Identity-lifecycle events (tier upgrade, mandate signed) still go via
    # structlog (audit_service.py) — those don't fit a per-action audit row even
    # with relaxed FKs. See migration 8df1d6891fd9.
    agent_id = Column(UUID(as_uuid=False), nullable=True)
    mandate_id = Column(UUID(as_uuid=False), nullable=True)

    action = Column(String(50), nullable=False)  # create_intent, send_offer, accept, ...
    params = Column(JSONB)
    result = Column(JSONB)
    success = Column(Boolean, nullable=False)
    error_code = Column(String(50), nullable=True)

    # 7.1.5 — IPv6-max length (45 = "ffff:ffff:..." with embedded IPv4
    # tail or a `%zone` suffix). Set on every security/abuse event;
    # legacy intent/agent rows leave it NULL.
    actor_ip = Column(String(45), nullable=True)

    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_audit_user_time", "user_id", "timestamp"),
        Index("ix_audit_agent_time", "agent_id", "timestamp"),
        # 7.1.5 — supports the sequential-email detection query
        # (`WHERE action='register_complete' AND timestamp > now-24h`).
        Index("ix_audit_action_time", "action", "timestamp"),
    )


class DailyCostTracking(Base):
    """Per-user daily LLM cost (brief task 6.3.c, expanded in 7.3.2).

    Composite PK `(date, user_id)`. UPSERTed by the orchestrator after
    each tick (`INSERT ... ON CONFLICT (date, user_id) DO UPDATE`).

    Two cap layers read from this table:
      - **Hard cap (global)**: scheduler sums `total_cost_usd` across
        all users for today; if `>= settings.max_daily_llm_cost_usd`
        the discovery cycle skips dispatching for the rest of the UTC
        day. Kill-switch protecting against runaway/infinite-loop bugs.
      - **Soft cap (per-user)**: scheduler reads single-row
        `total_cost_usd` for `(today, user_id)` before each candidate
        dispatch; if `>= settings.daily_user_cost_cap_usd` that user's
        tick is skipped. Other users continue normally. Protection
        against single-user blow-up scenarios.

    Storage: O(distinct users active per day). Negligible at V0 alpha
    (~10 users) and remains tractable through V0.5+ (~10K users → 3.6M
    rows/year, still small).
    """
    __tablename__ = "daily_cost_tracking"

    date = Column(Date, primary_key=True)
    user_id = Column(UUID(as_uuid=False), primary_key=True)
    total_cost_usd = Column(
        Numeric(12, 6), nullable=False, default=0, server_default=text("0")
    )
    tick_count = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        # Inverse-order index for the per-user soft-cap path
        # (`get_user_cost_today`): the composite PK starts with date for
        # the SUM-cross-users hard-cap query, this one starts with user_id
        # for the per-user lookup.
        Index("ix_daily_cost_user_date", "user_id", "date"),
    )


# ============================================================================
# KMS LAYER
# ============================================================================


class KMSAgentKey(Base):
    """Per-agent ed25519 privkey, AES-256-GCM-encrypted at rest ([7.4.1]).

    Owned exclusively by `app.services.kms.local_db_provider.LocalDBProvider`.
    No other service reads this table; callers see only the opaque
    `Agent.privkey_kms_ref` string ("db:<id>") returned from `generate_agent_keypair`.

    No FK from here back to Agent (or vice versa): the kms_ref is opaque from
    the Agent's perspective, mirroring how a future `aws:<arn>` ref would also
    have no relational tie. Trade-off: cascade delete is not automatic — if
    Agent rows are ever deleted (V0.5+), an explicit `revoke()` hook will need
    to clean these rows. V0 has no Agent deletion path, so the orphan risk is
    deferred (entry in IDEAS_BACKLOG).
    """
    __tablename__ = "kms_agent_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # AES-GCM ciphertext of the 32-byte ed25519 private key. Tag is appended.
    privkey_encrypted = Column(LargeBinary, nullable=False)
    # 12-byte random nonce; never reused with the same master key.
    nonce = Column(LargeBinary, nullable=False)
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        server_default=func.now(),
    )


# ============================================================================
# AUTH TOKENS LAYER
# ============================================================================


class RefreshToken(Base):
    """Server-side refresh token state ([7.4.2]).

    Refresh tokens are opaque random strings (`secrets.token_urlsafe(32)`);
    only their SHA-256 hex digest is stored, so a DB compromise does not yield
    usable tokens. Each consume rotates: the row flips to `consumed`, a new
    `active` row is inserted with `parent_id` pointing at the consumed one.

    A reuse hit (a `consumed` row presented again) is treated as a compromise
    signal — the V0 response is to revoke every active/consumed token for the
    user. Chain-only invalidation via recursive CTE is deferred to V0.5+ when
    multi-device sessions become real (entry in IDEAS_BACKLOG).
    """
    __tablename__ = "refresh_tokens"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # SHA-256 hex digest of the opaque token; 64 chars.
    token_hash = Column(String(64), nullable=False, unique=True)
    # Self-FK back to the row this token rotated from (None on the initial
    # token issued at register/login).
    parent_id = Column(
        UUID(as_uuid=False),
        ForeignKey("refresh_tokens.id"),
        nullable=True,
    )
    # 'active' (usable) | 'consumed' (rotated, retained for reuse detection)
    # | 'revoked' (explicitly invalidated, e.g. reuse hit on the chain).
    status = Column(
        String(20),
        nullable=False,
        default="active",
        server_default=text("'active'"),
    )
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        server_default=func.now(),
    )
    # Lifecycle endpoint timestamp: set when status flips to 'consumed' (rotation)
    # OR 'revoked' (explicit revoke / reuse-cascade). One column for both since
    # 'active → consumed → revoked' is monotonic — no terminal state can be
    # re-entered.
    consumed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # Active sessions per user (V0.5+ "logout all devices" reads this).
        Index(
            "ix_refresh_tokens_user_active",
            "user_id",
            postgresql_where=text("status = 'active'"),
        ),
    )
