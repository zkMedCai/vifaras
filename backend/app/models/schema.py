"""
Schema database del marketplace.

Principio guida: ZERO PII memorizzata.
Identità = nullifier opaco da Self Protocol.
"""
from datetime import datetime
from enum import Enum
from sqlalchemy import (
    Column, String, Integer, Numeric, DateTime, Boolean,
    ForeignKey, Text, JSON, BigInteger, Index, UniqueConstraint, LargeBinary
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
    tier = Column(Integer, nullable=False, default=0)

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
    consumed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_mandate_drafts_user_expires", "user_id", "expires_at"),
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
    agent_id = Column(UUID(as_uuid=False), ForeignKey("agents.id"), nullable=False)
    
    side = Column(String(4), nullable=False)  # 'buy' | 'sell'
    
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
    )


class Match(Base):
    """Match potenziale tra un buy_intent e un sell_intent."""
    __tablename__ = "matches"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    buy_intent_id = Column(UUID(as_uuid=False), ForeignKey("intents.id"), nullable=False)
    sell_intent_id = Column(UUID(as_uuid=False), ForeignKey("intents.id"), nullable=False)
    
    similarity_score = Column(Numeric(5, 4))  # 0.0000-1.0000
    price_overlap = Column(Boolean, default=False)
    
    status = Column(String(20), default="discovered")  # discovered|negotiating|agreed|rejected|expired
    created_at = Column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint("buy_intent_id", "sell_intent_id", name="uq_match"),
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
    """
    __tablename__ = "deals"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    negotiation_id = Column(UUID(as_uuid=False), ForeignKey("negotiations.id"), nullable=False)
    
    buyer_user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    seller_user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    buy_intent_id = Column(UUID(as_uuid=False), ForeignKey("intents.id"), nullable=False)
    sell_intent_id = Column(UUID(as_uuid=False), ForeignKey("intents.id"), nullable=False)
    
    final_price_cents = Column(BigInteger, nullable=False)
    
    # Step-up signatures (passkey-firmate dagli umani)
    buyer_signature = Column(JSONB, nullable=True)
    buyer_signed_at = Column(DateTime, nullable=True)
    seller_signature = Column(JSONB, nullable=True)
    seller_signed_at = Column(DateTime, nullable=True)
    
    status = Column(String(20), default="pending_buyer")
    # pending_buyer -> pending_seller -> confirmed -> completed -> disputed/cancelled
    
    created_at = Column(DateTime, default=datetime.utcnow)
    confirmed_at = Column(DateTime, nullable=True)
    
    # EC5: idempotency key per evitare double-deal su stessa negoziazione
    idempotency_key = Column(Text, unique=True, nullable=False)


class DealMessage(Base):
    """Chat tra umani post-deal per coordinare consegna/pagamento. Pseudonimi."""
    __tablename__ = "deal_messages"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    deal_id = Column(UUID(as_uuid=False), ForeignKey("deals.id"), nullable=False)
    sender_user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"), nullable=False)
    
    encrypted_content = Column(Text, nullable=False)  # E2E cifrato
    created_at = Column(DateTime, default=datetime.utcnow)


# ============================================================================
# AUDIT LAYER
# ============================================================================

class AuditLog(Base):
    """
    Log immutabile di ogni azione agente.
    Naturalmente pseudonimo (user_id punta a nullifier).
    AI Act compliant by design.
    """
    __tablename__ = "audit_log"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    
    user_id = Column(UUID(as_uuid=False), nullable=False)  # no FK per immutabilità
    agent_id = Column(UUID(as_uuid=False), nullable=False)
    mandate_id = Column(UUID(as_uuid=False), nullable=False)
    
    action = Column(String(50), nullable=False)  # create_intent, send_offer, accept, ...
    params = Column(JSONB)
    result = Column(JSONB)
    success = Column(Boolean, nullable=False)
    error_code = Column(String(50), nullable=True)
    
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    __table_args__ = (
        Index("ix_audit_user_time", "user_id", "timestamp"),
        Index("ix_audit_agent_time", "agent_id", "timestamp"),
    )
