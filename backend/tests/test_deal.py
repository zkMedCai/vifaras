"""Deal service + chat tests (brief task 5.3).

32 tests organized by concern:

  Creation (5):
   1. accept_offer creates a pending Deal with correct fields
   2. deal creation idempotent via natural key
   3. deal links to negotiation + intents
   4. expires_at is ~24h from creation
   5. status initially 'pending_signatures'

  Sign draft (5):
   6. buyer can request sign draft
   7. seller can request sign draft
   8. non-party cannot request draft (403)
   9. already-signed party cannot request second sign draft (409)
  10. draft for non-pending deal fails (cancelled/confirmed/expired)

  Sign submit (8):
  11. valid signature marks party signed
  12. first signature does NOT confirm deal
  13. both signatures confirm deal + set confirmed_at
  14. invalid signature fails, no state change
  15. expired draft fails (410)
  16. consumed draft fails (409)
  17. replay protection via consumed flag
  18. audit logs SIGN per role + CONFIRM separately

  Cancel (4):
  19. cancel with valid signature succeeds
  20. cancel rolls back intent state to 'active'
  21. cancel resets chosen match to 'discovered'
  22. cancel after confirm fails (409)

  Expiration (3):
  23. pending deal past expires_at gets expired by scheduler
  24. expired deal rolls back intent state
  25. partially-signed deal still expires

  Chat (4):
  26. send message to confirmed deal succeeds
  27. send message to pending deal fails (deal not confirmed)
  28. non-party cannot send messages
  29. message size capped at 4 KB

  Concurrency (3):
  30. only one role can sign at a time (serialize via lock)
  31. cancel on already-cancelled deal is rejected
  32. expiration during sign returns 410 (deal expired mid-flow)
"""
from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.models.schema import (
    AuditLog,
    Deal,
    DealMessage,
    DealSignatureDraft,
    Intent,
    Match,
    Negotiation,
    User,
)
from app.services import (
    audit_service,
    deal_message_service,
    deal_service,
    embedding_service,
    negotiation_service,
)
from tests.factories import default_user_kwargs, setup_active_mandate_async


# ---------------------------------------------------------------------------
# Module fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_fake_embedding(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "fake")
    embedding_service._reset_singleton_for_tests()
    yield
    embedding_service._reset_singleton_for_tests()


@pytest.fixture
def webauthn_ok(monkeypatch):
    """Patch verify_authentication_response to always succeed.

    Test bodies that need failure mock can override locally.
    """
    monkeypatch.setattr(
        "app.services.deal_service.verify_authentication_response",
        lambda **_: SimpleNamespace(new_sign_count=1),
    )


def _patch_webauthn_raise(monkeypatch, message: str = "bad signature") -> None:
    def _raise(**_: Any) -> None:
        raise Exception(message)

    monkeypatch.setattr(
        "app.services.deal_service.verify_authentication_response", _raise
    )


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


@dataclass
class DealSetup:
    seller_user_id: str
    seller_agent_id: str
    buyer_user_id: str
    buyer_agent_id: str
    sell_intent_id: str
    buy_intent_id: str
    match_id: str
    negotiation_id: str
    deal_id: str


async def _seed_intent(
    db,
    *,
    user_id: str,
    side: str,
    seed_text: str = "macbook",
    reservation_eur: float = 1000,
    ideal_eur: float = 1100,
) -> str:
    intent_id = str(uuid.uuid4())
    now = datetime.utcnow()
    intent = Intent(
        id=intent_id,
        user_id=user_id,
        agent_id=None,
        side=side,
        title=f"intent-{intent_id[:6]}",
        description=seed_text,
        category="electronics_laptops",
        description_embedding=embedding_service._fake_embedding(seed_text),
        reservation_price_cents=int(reservation_eur * 100),
        ideal_price_cents=int(ideal_eur * 100),
        currency="EUR",
        hard_constraints={},
        soft_preferences={},
        status="active",
        expires_at=now + timedelta(days=14),
        created_at=now,
    )
    db.add(intent)
    await db.commit()
    return intent_id


async def _seed_match(db, *, buy_intent_id: str, sell_intent_id: str) -> str:
    m = Match(
        id=str(uuid.uuid4()),
        buy_intent_id=buy_intent_id,
        sell_intent_id=sell_intent_id,
        similarity_score=0.95,
        price_overlap=True,
        price_proximity_score=0.85,
        combined_score=0.92,
        status="discovered",
    )
    db.add(m)
    await db.commit()
    return m.id


async def _seed_pending_deal(db) -> DealSetup:
    """Run accept_offer to produce a pending Deal. Returns full setup."""
    seller_id, seller_agent, _ = await setup_active_mandate_async(
        db, email=f"sell-{uuid.uuid4().hex[:6]}@x.com"
    )
    buyer_id, buyer_agent, _ = await setup_active_mandate_async(
        db, email=f"buy-{uuid.uuid4().hex[:6]}@x.com"
    )
    sell_id = await _seed_intent(
        db,
        user_id=seller_id,
        side="sell",
        reservation_eur=1000,
        ideal_eur=1200,
    )
    buy_id = await _seed_intent(
        db,
        user_id=buyer_id,
        side="buy",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    match_id = await _seed_match(
        db, buy_intent_id=buy_id, sell_intent_id=sell_id
    )

    nego = await negotiation_service.start_or_continue(
        db,
        user_id=seller_id,
        agent_id=seller_agent,
        match_id=match_id,
        price_cents=120000,
    )
    accept = await negotiation_service.accept_offer(
        db,
        user_id=buyer_id,
        agent_id=buyer_agent,
        negotiation_id=nego.negotiation_id,
    )
    assert accept.deal_id is not None

    return DealSetup(
        seller_user_id=seller_id,
        seller_agent_id=seller_agent,
        buyer_user_id=buyer_id,
        buyer_agent_id=buyer_agent,
        sell_intent_id=sell_id,
        buy_intent_id=buy_id,
        match_id=match_id,
        negotiation_id=nego.negotiation_id,
        deal_id=accept.deal_id,
    )


def _bearer(client, user_id: str, tier: int) -> None:
    client.headers["Authorization"] = (
        f"Bearer {create_access_token(user_id=user_id, tier=tier)}"
    )


def _fake_assertion() -> dict[str, Any]:
    return {
        "id": "Zm9v",
        "rawId": "Zm9v",
        "type": "public-key",
        "response": {
            "authenticatorData": "ad",
            "clientDataJSON": "cd",
            "signature": "sg",
            "userHandle": "uh",
        },
    }


# ===========================================================================
# 1. accept_offer creates a pending Deal with correct fields
# ===========================================================================


@pytest.mark.db
async def test_accept_creates_pending_deal_with_correct_fields(
    async_db_session,
) -> None:
    s = await _seed_pending_deal(async_db_session)
    deal = await async_db_session.get(Deal, s.deal_id)
    assert deal is not None
    assert deal.status == "pending_signatures"
    assert deal.agreed_price_cents == 120000
    assert deal.currency == "EUR"
    assert deal.buyer_user_id == s.buyer_user_id
    assert deal.seller_user_id == s.seller_user_id
    assert deal.buyer_signature is None
    assert deal.seller_signature is None


# ===========================================================================
# 2. deal creation idempotent via natural key
# ===========================================================================


@pytest.mark.db
async def test_deal_creation_idempotent_via_natural_key(
    async_db_session,
) -> None:
    s = await _seed_pending_deal(async_db_session)
    # Calling create_pending_deal again with the same key returns the same row.
    deal2 = await deal_service.create_pending_deal(
        async_db_session,
        negotiation_id=s.negotiation_id,
        buy_intent_id=s.buy_intent_id,
        sell_intent_id=s.sell_intent_id,
        buyer_user_id=s.buyer_user_id,
        seller_user_id=s.seller_user_id,
        agreed_price_cents=120000,
    )
    assert deal2.id == s.deal_id


# ===========================================================================
# 3. deal links to negotiation + intents
# ===========================================================================


@pytest.mark.db
async def test_deal_links_to_negotiation_and_intents(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    deal = await async_db_session.get(Deal, s.deal_id)
    assert deal.negotiation_id == s.negotiation_id
    assert deal.buy_intent_id == s.buy_intent_id
    assert deal.sell_intent_id == s.sell_intent_id


# ===========================================================================
# 4. expires_at is ~24h from creation
# ===========================================================================


@pytest.mark.db
async def test_expires_at_24h_from_creation(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    deal = await async_db_session.get(Deal, s.deal_id)
    delta = deal.expires_at - deal.created_at
    # 24h ± a few seconds tolerance.
    assert abs(delta.total_seconds() - 86400) < 5


# ===========================================================================
# 5. status initially 'pending_signatures'
# ===========================================================================


@pytest.mark.db
async def test_status_initially_pending_signatures(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    deal = await async_db_session.get(Deal, s.deal_id)
    assert deal.status == "pending_signatures"


# ===========================================================================
# 6. buyer can request sign draft
# ===========================================================================


@pytest.mark.db
async def test_buyer_can_request_sign_draft(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    assert draft.role == "buyer"
    assert draft.kind == "sign"
    assert draft.payload["deal_id"] == s.deal_id
    assert draft.payload["agreed_price_cents"] == 120000


# ===========================================================================
# 7. seller can request sign draft
# ===========================================================================


@pytest.mark.db
async def test_seller_can_request_sign_draft(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.seller_user_id, deal_id=s.deal_id
    )
    assert draft.role == "seller"


# ===========================================================================
# 8. non-party cannot request draft
# ===========================================================================


@pytest.mark.db
async def test_non_party_cannot_request_draft(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    outsider, _, _ = await setup_active_mandate_async(
        async_db_session, email=f"out-{uuid.uuid4().hex[:6]}@x.com"
    )
    with pytest.raises(deal_service.NotPartyToDeal):
        await deal_service.request_sign_draft(
            async_db_session, user_id=outsider, deal_id=s.deal_id
        )


# ===========================================================================
# 9. already-signed party cannot request second sign draft
# ===========================================================================


@pytest.mark.db
async def test_already_signed_party_cannot_request_second_draft(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    from app.services.mandate_service import WebAuthnAssertionPayload

    assertion = WebAuthnAssertionPayload(**_fake_assertion())
    await deal_service.submit_signature(
        async_db_session,
        user_id=s.buyer_user_id,
        draft_id=draft.draft_id,
        assertion=assertion,
    )
    with pytest.raises(deal_service.AlreadySigned):
        await deal_service.request_sign_draft(
            async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
        )


# ===========================================================================
# 10. draft for cancelled deal fails
# ===========================================================================


@pytest.mark.db
async def test_draft_for_cancelled_deal_fails(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    deal = await async_db_session.get(Deal, s.deal_id)
    deal.status = "cancelled"
    await async_db_session.commit()
    with pytest.raises(deal_service.DealNotPending):
        await deal_service.request_sign_draft(
            async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
        )


# ===========================================================================
# 11. valid signature marks party signed
# ===========================================================================


@pytest.mark.db
async def test_valid_signature_marks_party_signed(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    from app.services.mandate_service import WebAuthnAssertionPayload

    result = await deal_service.submit_signature(
        async_db_session,
        user_id=s.buyer_user_id,
        draft_id=draft.draft_id,
        assertion=WebAuthnAssertionPayload(**_fake_assertion()),
    )
    assert result.role == "buyer"
    assert result.deal_confirmed is False
    deal = await async_db_session.get(Deal, s.deal_id)
    await async_db_session.refresh(deal)
    assert deal.buyer_signed_at is not None
    assert deal.buyer_signature is not None
    assert deal.seller_signature is None


# ===========================================================================
# 12. first signature does NOT confirm deal
# ===========================================================================


@pytest.mark.db
async def test_first_signature_does_not_confirm(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    from app.services.mandate_service import WebAuthnAssertionPayload

    result = await deal_service.submit_signature(
        async_db_session,
        user_id=s.buyer_user_id,
        draft_id=draft.draft_id,
        assertion=WebAuthnAssertionPayload(**_fake_assertion()),
    )
    assert result.deal_confirmed is False
    deal = await async_db_session.get(Deal, s.deal_id)
    await async_db_session.refresh(deal)
    assert deal.status == "pending_signatures"
    assert deal.confirmed_at is None


# ===========================================================================
# 13. both signatures confirm deal + set confirmed_at
# ===========================================================================


@pytest.mark.db
async def test_both_signatures_confirm_deal(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    from app.services.mandate_service import WebAuthnAssertionPayload

    for user_id in (s.buyer_user_id, s.seller_user_id):
        draft = await deal_service.request_sign_draft(
            async_db_session, user_id=user_id, deal_id=s.deal_id
        )
        await deal_service.submit_signature(
            async_db_session,
            user_id=user_id,
            draft_id=draft.draft_id,
            assertion=WebAuthnAssertionPayload(**_fake_assertion()),
        )

    deal = await async_db_session.get(Deal, s.deal_id)
    await async_db_session.refresh(deal)
    assert deal.status == "confirmed"
    assert deal.confirmed_at is not None


# ===========================================================================
# 14. invalid signature fails, no state change
# ===========================================================================


@pytest.mark.db
async def test_invalid_signature_no_state_change(
    async_db_session, monkeypatch
) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    _patch_webauthn_raise(monkeypatch, "invalid sig")
    from app.services.mandate_service import WebAuthnAssertionPayload

    with pytest.raises(deal_service.DealWebAuthnVerificationFailed):
        await deal_service.submit_signature(
            async_db_session,
            user_id=s.buyer_user_id,
            draft_id=draft.draft_id,
            assertion=WebAuthnAssertionPayload(**_fake_assertion()),
        )
    # Deal untouched.
    deal = await async_db_session.get(Deal, s.deal_id)
    await async_db_session.refresh(deal)
    assert deal.buyer_signed_at is None
    assert deal.status == "pending_signatures"


# ===========================================================================
# 15. expired draft fails (410)
# ===========================================================================


@pytest.mark.db
async def test_expired_draft_fails(async_db_session, webauthn_ok) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    # Force the draft to be in the past.
    draft_row = await async_db_session.get(DealSignatureDraft, draft.draft_id)
    draft_row.expires_at = datetime.utcnow() - timedelta(minutes=1)
    await async_db_session.commit()

    from app.services.mandate_service import WebAuthnAssertionPayload

    with pytest.raises(deal_service.DealDraftExpired):
        await deal_service.submit_signature(
            async_db_session,
            user_id=s.buyer_user_id,
            draft_id=draft.draft_id,
            assertion=WebAuthnAssertionPayload(**_fake_assertion()),
        )


# ===========================================================================
# 16. consumed draft fails (replay)
# ===========================================================================


@pytest.mark.db
async def test_consumed_draft_fails(async_db_session, webauthn_ok) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    from app.services.mandate_service import WebAuthnAssertionPayload

    await deal_service.submit_signature(
        async_db_session,
        user_id=s.buyer_user_id,
        draft_id=draft.draft_id,
        assertion=WebAuthnAssertionPayload(**_fake_assertion()),
    )
    # Replay attempt.
    with pytest.raises(deal_service.DealDraftAlreadyConsumed):
        await deal_service.submit_signature(
            async_db_session,
            user_id=s.buyer_user_id,
            draft_id=draft.draft_id,
            assertion=WebAuthnAssertionPayload(**_fake_assertion()),
        )


# ===========================================================================
# 17. replay protection via consumed flag (verify column)
# ===========================================================================


@pytest.mark.db
async def test_replay_protection_via_consumed_flag(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    from app.services.mandate_service import WebAuthnAssertionPayload

    await deal_service.submit_signature(
        async_db_session,
        user_id=s.buyer_user_id,
        draft_id=draft.draft_id,
        assertion=WebAuthnAssertionPayload(**_fake_assertion()),
    )
    draft_row = await async_db_session.get(DealSignatureDraft, draft.draft_id)
    await async_db_session.refresh(draft_row)
    assert draft_row.consumed is True


# ===========================================================================
# 18. audit logs SIGN per role + CONFIRM separately
# ===========================================================================


@pytest.mark.db
async def test_audit_logs_sign_and_confirm_separately(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    from app.services.mandate_service import WebAuthnAssertionPayload

    for user_id in (s.buyer_user_id, s.seller_user_id):
        draft = await deal_service.request_sign_draft(
            async_db_session, user_id=user_id, deal_id=s.deal_id
        )
        await deal_service.submit_signature(
            async_db_session,
            user_id=user_id,
            draft_id=draft.draft_id,
            assertion=WebAuthnAssertionPayload(**_fake_assertion()),
        )

    rows = list(
        await async_db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action.in_((
                audit_service.DealActions.BUYER_SIGN,
                audit_service.DealActions.SELLER_SIGN,
                audit_service.DealActions.CONFIRM,
            )))
            .where(AuditLog.params["deal_id"].astext == s.deal_id)
        )
    )
    actions = {r.action for r in rows}
    assert audit_service.DealActions.BUYER_SIGN in actions
    assert audit_service.DealActions.SELLER_SIGN in actions
    assert audit_service.DealActions.CONFIRM in actions


# ===========================================================================
# 19. cancel with valid signature succeeds
# ===========================================================================


@pytest.mark.db
async def test_cancel_with_valid_signature(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_cancel_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    from app.services.mandate_service import WebAuthnAssertionPayload

    result = await deal_service.submit_cancel(
        async_db_session,
        user_id=s.buyer_user_id,
        draft_id=draft.draft_id,
        assertion=WebAuthnAssertionPayload(**_fake_assertion()),
    )
    assert result.intents_reverted == 2
    assert result.matches_reverted == 1
    deal = await async_db_session.get(Deal, s.deal_id)
    await async_db_session.refresh(deal)
    assert deal.status == "cancelled"


# ===========================================================================
# 20. cancel rolls back intent state to 'active'
# ===========================================================================


@pytest.mark.db
async def test_cancel_rolls_back_intents(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_cancel_draft(
        async_db_session, user_id=s.seller_user_id, deal_id=s.deal_id
    )
    from app.services.mandate_service import WebAuthnAssertionPayload

    await deal_service.submit_cancel(
        async_db_session,
        user_id=s.seller_user_id,
        draft_id=draft.draft_id,
        assertion=WebAuthnAssertionPayload(**_fake_assertion()),
    )
    sell_intent = await async_db_session.get(Intent, s.sell_intent_id)
    buy_intent = await async_db_session.get(Intent, s.buy_intent_id)
    await async_db_session.refresh(sell_intent)
    await async_db_session.refresh(buy_intent)
    assert sell_intent.status == "active"
    assert buy_intent.status == "active"


# ===========================================================================
# 21. cancel resets chosen match to 'discovered'
# ===========================================================================


@pytest.mark.db
async def test_cancel_resets_match_to_discovered(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_cancel_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    from app.services.mandate_service import WebAuthnAssertionPayload

    await deal_service.submit_cancel(
        async_db_session,
        user_id=s.buyer_user_id,
        draft_id=draft.draft_id,
        assertion=WebAuthnAssertionPayload(**_fake_assertion()),
    )
    match = await async_db_session.get(Match, s.match_id)
    await async_db_session.refresh(match)
    assert match.status == "discovered"


# ===========================================================================
# 22. cancel after confirm fails (V0 no post-confirm cancel)
# ===========================================================================


@pytest.mark.db
async def test_cancel_after_confirm_fails(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    from app.services.mandate_service import WebAuthnAssertionPayload

    # Confirm the deal first.
    for user_id in (s.buyer_user_id, s.seller_user_id):
        draft = await deal_service.request_sign_draft(
            async_db_session, user_id=user_id, deal_id=s.deal_id
        )
        await deal_service.submit_signature(
            async_db_session,
            user_id=user_id,
            draft_id=draft.draft_id,
            assertion=WebAuthnAssertionPayload(**_fake_assertion()),
        )

    with pytest.raises(deal_service.CannotCancelConfirmedDeal):
        await deal_service.request_cancel_draft(
            async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
        )


# ===========================================================================
# 23. expired deal: scheduler tick marks it
# ===========================================================================


@pytest.mark.db
async def test_pending_deal_expires(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    deal = await async_db_session.get(Deal, s.deal_id)
    deal.expires_at = datetime.utcnow() - timedelta(minutes=1)
    await async_db_session.commit()

    result = await deal_service.expire_deal(
        async_db_session, deal_id=s.deal_id
    )
    assert result.intents_reverted == 2
    assert result.matches_reverted == 1
    await async_db_session.refresh(deal)
    assert deal.status == "expired"


# ===========================================================================
# 24. expired deal rolls back intents
# ===========================================================================


@pytest.mark.db
async def test_expired_deal_rolls_back_intents(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    await deal_service.expire_deal(async_db_session, deal_id=s.deal_id)
    sell_intent = await async_db_session.get(Intent, s.sell_intent_id)
    buy_intent = await async_db_session.get(Intent, s.buy_intent_id)
    await async_db_session.refresh(sell_intent)
    await async_db_session.refresh(buy_intent)
    assert sell_intent.status == "active"
    assert buy_intent.status == "active"


# ===========================================================================
# 25. partially-signed deal still expires
# ===========================================================================


@pytest.mark.db
async def test_partially_signed_deal_still_expires(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    # Buyer signs, seller doesn't.
    from app.services.mandate_service import WebAuthnAssertionPayload

    draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    await deal_service.submit_signature(
        async_db_session,
        user_id=s.buyer_user_id,
        draft_id=draft.draft_id,
        assertion=WebAuthnAssertionPayload(**_fake_assertion()),
    )
    # Force expiry.
    result = await deal_service.expire_deal(
        async_db_session, deal_id=s.deal_id
    )
    assert result.intents_reverted == 2
    deal = await async_db_session.get(Deal, s.deal_id)
    await async_db_session.refresh(deal)
    assert deal.status == "expired"


# ===========================================================================
# 26. send message to confirmed deal succeeds
# ===========================================================================


@pytest.mark.db
async def test_send_message_to_confirmed_deal(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    from app.services.mandate_service import WebAuthnAssertionPayload

    for user_id in (s.buyer_user_id, s.seller_user_id):
        draft = await deal_service.request_sign_draft(
            async_db_session, user_id=user_id, deal_id=s.deal_id
        )
        await deal_service.submit_signature(
            async_db_session,
            user_id=user_id,
            draft_id=draft.draft_id,
            assertion=WebAuthnAssertionPayload(**_fake_assertion()),
        )

    msg = await deal_message_service.send_message(
        async_db_session,
        user_id=s.buyer_user_id,
        deal_id=s.deal_id,
        encrypted_content=b"hello-encrypted",
        nonce=b"x" * 12,
    )
    assert msg.id
    assert msg.sender_user_id == s.buyer_user_id


# ===========================================================================
# 27. send message to pending deal fails
# ===========================================================================


@pytest.mark.db
async def test_send_message_to_pending_deal_fails(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    with pytest.raises(deal_service.DealNotConfirmed):
        await deal_message_service.send_message(
            async_db_session,
            user_id=s.buyer_user_id,
            deal_id=s.deal_id,
            encrypted_content=b"x",
            nonce=b"y" * 12,
        )


# ===========================================================================
# 28. non-party cannot send messages
# ===========================================================================


@pytest.mark.db
async def test_non_party_cannot_send_messages(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    from app.services.mandate_service import WebAuthnAssertionPayload

    # Confirm the deal so chat is open.
    for user_id in (s.buyer_user_id, s.seller_user_id):
        draft = await deal_service.request_sign_draft(
            async_db_session, user_id=user_id, deal_id=s.deal_id
        )
        await deal_service.submit_signature(
            async_db_session,
            user_id=user_id,
            draft_id=draft.draft_id,
            assertion=WebAuthnAssertionPayload(**_fake_assertion()),
        )

    outsider, _, _ = await setup_active_mandate_async(
        async_db_session, email=f"out-{uuid.uuid4().hex[:6]}@x.com"
    )
    with pytest.raises(deal_service.NotPartyToDeal):
        await deal_message_service.send_message(
            async_db_session,
            user_id=outsider,
            deal_id=s.deal_id,
            encrypted_content=b"x",
            nonce=b"y" * 12,
        )


# ===========================================================================
# 29. message size capped at 4 KB
# ===========================================================================


@pytest.mark.db
async def test_message_size_capped(async_db_session, webauthn_ok) -> None:
    s = await _seed_pending_deal(async_db_session)
    # Confirm.
    from app.services.mandate_service import WebAuthnAssertionPayload

    for user_id in (s.buyer_user_id, s.seller_user_id):
        draft = await deal_service.request_sign_draft(
            async_db_session, user_id=user_id, deal_id=s.deal_id
        )
        await deal_service.submit_signature(
            async_db_session,
            user_id=user_id,
            draft_id=draft.draft_id,
            assertion=WebAuthnAssertionPayload(**_fake_assertion()),
        )

    too_big = b"X" * (deal_message_service.MAX_MESSAGE_BYTES + 1)
    with pytest.raises(deal_message_service.MessageTooLarge):
        await deal_message_service.send_message(
            async_db_session,
            user_id=s.buyer_user_id,
            deal_id=s.deal_id,
            encrypted_content=too_big,
            nonce=b"z" * 12,
        )


# ===========================================================================
# 30. only one role can sign at a time (lock serializes)
# ===========================================================================


@pytest.mark.db
async def test_two_signatures_serialize(async_db_session, webauthn_ok) -> None:
    """Two sign drafts (buyer + seller) submitted in sequence — both succeed
    and the deal confirms. The lock serializes the writes; we don't test
    true concurrency (same caveat as 5.1/5.2).
    """
    s = await _seed_pending_deal(async_db_session)
    from app.services.mandate_service import WebAuthnAssertionPayload

    b_draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    sl_draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.seller_user_id, deal_id=s.deal_id
    )
    await deal_service.submit_signature(
        async_db_session,
        user_id=s.buyer_user_id,
        draft_id=b_draft.draft_id,
        assertion=WebAuthnAssertionPayload(**_fake_assertion()),
    )
    final = await deal_service.submit_signature(
        async_db_session,
        user_id=s.seller_user_id,
        draft_id=sl_draft.draft_id,
        assertion=WebAuthnAssertionPayload(**_fake_assertion()),
    )
    assert final.deal_confirmed is True


# ===========================================================================
# 31. cancel on already-cancelled deal is rejected
# ===========================================================================


@pytest.mark.db
async def test_cancel_on_already_cancelled_deal_rejected(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_cancel_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    from app.services.mandate_service import WebAuthnAssertionPayload

    await deal_service.submit_cancel(
        async_db_session,
        user_id=s.buyer_user_id,
        draft_id=draft.draft_id,
        assertion=WebAuthnAssertionPayload(**_fake_assertion()),
    )
    # Now request another cancel draft.
    with pytest.raises(deal_service.DealNotPending):
        await deal_service.request_cancel_draft(
            async_db_session, user_id=s.seller_user_id, deal_id=s.deal_id
        )


# ===========================================================================
# 32. expiration during sign returns 410
# ===========================================================================


@pytest.mark.db
async def test_expiration_during_sign_returns_410(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    draft = await deal_service.request_sign_draft(
        async_db_session, user_id=s.buyer_user_id, deal_id=s.deal_id
    )
    # Force the deal to expired between draft creation and submit.
    deal = await async_db_session.get(Deal, s.deal_id)
    deal.status = "expired"
    await async_db_session.commit()

    from app.services.mandate_service import WebAuthnAssertionPayload

    with pytest.raises(deal_service.DealAlreadyExpired):
        await deal_service.submit_signature(
            async_db_session,
            user_id=s.buyer_user_id,
            draft_id=draft.draft_id,
            assertion=WebAuthnAssertionPayload(**_fake_assertion()),
        )
