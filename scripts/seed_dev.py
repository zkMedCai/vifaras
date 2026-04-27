"""Idempotent dev seed for the marketplace local DB.

Creates a fixed set of fixtures so downstream code (services, tests, manual
exploration) has something realistic to work against without spending OpenAI
credits or running real Self Protocol verifications.

Fixtures
--------
- 3 users (alice / bob / carol) with distinct deterministic nullifier hashes.
- 1 agent per user (status=active, no mandate yet — mandate is task 2.4).
- 5 intents:
    * alice BUY  laptop      (electronics)
    * bob   SELL laptop      (electronics)   ← should match alice/laptop
    * carol SELL camera      (vintage_photo) ← no buyer counterpart
    * alice BUY  bike        (bikes)         ← no seller counterpart
    * bob   SELL monitor     (electronics)
- 1 manually-created Match (alice/laptop BUY ↔ bob/laptop SELL).

Embeddings are deterministic fakes derived from the description hash
(NOT semantically meaningful — see brief §1.2 note 3). No OpenAI call.

Run
---
    uv run python scripts/seed_dev.py

Re-runnable: deterministic uuid5 IDs + SELECT-before-INSERT, so the second
invocation is a no-op.
"""
from __future__ import annotations

import hashlib
import logging
import random
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import SyncSessionLocal
from app.models.schema import Agent, Intent, Match, User

# Deterministic namespace for uuid5() so re-runs produce the same row IDs.
# Hand-picked nibble pattern so seed-only rows are obviously not production data.
SEED_NS = uuid.UUID("00000000-0000-4000-8000-00d0e15eedde")


def did(label: str) -> str:
    return str(uuid.uuid5(SEED_NS, label))


def seed_nullifier(label: str) -> str:
    """Deterministic placeholder nullifier hash. Real ones come from Self at 2.3."""
    return "seed:" + hashlib.sha256(f"nullifier:{label}".encode()).hexdigest()


def fake_embedding(text: str, dim: int = 1536) -> list[float]:
    """Deterministic 1536-d 'embedding' from the text hash. Not semantic."""
    seed = int(hashlib.sha256(text.encode()).hexdigest(), 16)
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(dim)]


NOW = datetime(2026, 4, 27, 12, 0, 0)
EXPIRES_DOC = NOW + timedelta(days=365 * 5)
EXPIRES_INTENT = NOW + timedelta(days=7)

USER_LABELS = ("alice", "bob", "carol")

USERS = [
    {
        "label": label,
        "notification_email": f"{label}+seed@example.test",
    }
    for label in USER_LABELS
]

INTENTS = [
    {
        "key": "alice-buy-laptop",
        "user_label": "alice",
        "side": "buy",
        "title": "MacBook Pro 14 M3 16GB",
        "description": (
            "Cerco MacBook Pro 14 M3 base 16/512, condizioni come nuovo, "
            "Roma o spedizione assicurata."
        ),
        "category": "electronics",
        "reservation_price_cents": 1_600_00,
        "ideal_price_cents": 1_300_00,
    },
    {
        "key": "bob-sell-laptop",
        "user_label": "bob",
        "side": "sell",
        "title": "MacBook Pro 14 M3 16/512 — usato 6 mesi",
        "description": (
            "Vendo MacBook Pro 14 M3, scatola originale, batteria al 99%, "
            "ritiro Roma o spedizione."
        ),
        "category": "electronics",
        "reservation_price_cents": 1_200_00,
        "ideal_price_cents": 1_500_00,
    },
    {
        "key": "carol-sell-camera",
        "user_label": "carol",
        "side": "sell",
        "title": "Fotocamera vintage Pentax K1000",
        "description": (
            "Pentax K1000 con obiettivo 50mm 1.7, perfettamente funzionante, "
            "viene da una collezione."
        ),
        "category": "vintage_photo",
        "reservation_price_cents": 150_00,
        "ideal_price_cents": 220_00,
    },
    {
        "key": "alice-buy-bike",
        "user_label": "alice",
        "side": "buy",
        "title": "Bici da corsa taglia M",
        "description": (
            "Cerco bici da corsa taglia M, telaio carbonio, gruppo Shimano 105 "
            "minimo, budget contenuto."
        ),
        "category": "bikes",
        "reservation_price_cents": 800_00,
        "ideal_price_cents": 600_00,
    },
    {
        "key": "bob-sell-monitor",
        "user_label": "bob",
        "side": "sell",
        "title": "Monitor 4K LG 27UL850",
        "description": (
            "Monitor 27 4K USB-C 96W, garanzia residua 6 mesi, scatola originale."
        ),
        "category": "electronics",
        "reservation_price_cents": 250_00,
        "ideal_price_cents": 350_00,
    },
]

# alice/laptop BUY ↔ bob/laptop SELL — overlapping prices (cap €1600 ≥ floor €1200)
SEED_MATCH = {
    "key": "alice-bob-laptop",
    "buy_intent_key": "alice-buy-laptop",
    "sell_intent_key": "bob-sell-laptop",
    "similarity_score": 0.87,
    "price_overlap": True,
    "status": "discovered",
}


def upsert_user(db: Session, label: str, notification_email: str) -> User:
    user_id = did(f"user/{label}")
    existing = db.get(User, user_id)
    if existing is not None:
        return existing
    user = User(
        id=user_id,
        nullifier_hash=seed_nullifier(label),
        attributes_proven={"adult": True, "country": "IT", "valid": True},
        attributes_verified_at=NOW,
        attributes_expires_at=EXPIRES_DOC,
        passkey_credential_id=f"seed-cred-{label}",
        passkey_pubkey=f"seed-pubkey-{label}",
        passkey_sign_count=0,
        notification_email=notification_email,
        status="active",
        created_at=NOW,
        last_active_at=NOW,
    )
    db.add(user)
    db.flush()
    return user


def upsert_agent(db: Session, label: str) -> Agent:
    agent_id = did(f"agent/{label}")
    existing = db.get(Agent, agent_id)
    if existing is not None:
        return existing
    agent = Agent(
        id=agent_id,
        user_id=did(f"user/{label}"),
        name=f"{label.title()} Agent",
        pubkey=f"seed-agent-pubkey-{label}",
        privkey_kms_ref=f"seed-kms-ref-{label}",
        status="active",
        created_at=NOW,
    )
    db.add(agent)
    db.flush()
    return agent


def upsert_intent(db: Session, spec: dict) -> Intent:
    intent_id = did(f"intent/{spec['key']}")
    existing = db.get(Intent, intent_id)
    if existing is not None:
        return existing
    label = spec["user_label"]
    intent = Intent(
        id=intent_id,
        user_id=did(f"user/{label}"),
        agent_id=did(f"agent/{label}"),
        side=spec["side"],
        title=spec["title"],
        description=spec["description"],
        category=spec["category"],
        description_embedding=fake_embedding(spec["description"]),
        reservation_price_cents=spec["reservation_price_cents"],
        ideal_price_cents=spec["ideal_price_cents"],
        currency="EUR",
        hard_constraints={},
        soft_preferences={},
        status="active",
        expires_at=EXPIRES_INTENT,
        created_at=NOW,
    )
    db.add(intent)
    db.flush()
    return intent


def upsert_match(db: Session, spec: dict) -> Match:
    match_id = did(f"match/{spec['key']}")
    existing = db.get(Match, match_id)
    if existing is not None:
        return existing
    match = Match(
        id=match_id,
        buy_intent_id=did(f"intent/{spec['buy_intent_key']}"),
        sell_intent_id=did(f"intent/{spec['sell_intent_key']}"),
        similarity_score=spec["similarity_score"],
        price_overlap=spec["price_overlap"],
        status=spec["status"],
        created_at=NOW,
    )
    db.add(match)
    db.flush()
    return match


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("seed_dev")

    with SyncSessionLocal() as db:
        for spec in USERS:
            upsert_user(db, spec["label"], spec["notification_email"])
        for label in USER_LABELS:
            upsert_agent(db, label)
        for spec in INTENTS:
            upsert_intent(db, spec)
        upsert_match(db, SEED_MATCH)
        db.commit()

        counts = {
            "users": db.scalar(select(func.count()).select_from(User)),
            "agents": db.scalar(select(func.count()).select_from(Agent)),
            "intents": db.scalar(select(func.count()).select_from(Intent)),
            "matches": db.scalar(select(func.count()).select_from(Match)),
        }

    log.info(
        "seed complete: %d users, %d agents, %d intents, %d matches",
        counts["users"], counts["agents"], counts["intents"], counts["matches"],
    )


if __name__ == "__main__":
    main()
