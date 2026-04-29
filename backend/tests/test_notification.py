"""Notification service + integration tests (brief task 6.1).

20 tests organized by concern:

  Service core (5):
   1. create_notification persists row + log
   2. list filters unread_only
   3. list paginates via before_id cursor
   4. mark_read idempotent (second call returns False)
   5. cleanup_expired removes past-expiry rows

  Integration callsites (8):
   6. step_up_service.sign emits STEP_UP_APPROVED
   7. step_up_service.reject emits STEP_UP_REJECTED
   8. match_service.find_matches_for_intent emits NEW_MATCH for both parties
   9. negotiation_service.start_or_continue emits OFFER for counterparty
  10. negotiation_service.accept_offer emits DEAL_AWAITING_YOUR_SIGNATURE for both
  11. deal_service.submit_signature first sig emits OTHER_PARTY_SIGNED
  12. deal_service.submit_signature second sig emits DEAL_CONFIRMED for both
  13. deal_message_service.send_message emits DEAL_MESSAGE_RECEIVED for recipient

  Endpoint (5):
  14. GET /api/notifications returns caller's only
  15. GET /api/notifications/unread-count returns correct count
  16. POST /api/notifications/{id}/read flips read_at
  17. POST /api/notifications/{id}/acted flips acted_at + read_at
  18. POST /api/notifications/mark-all-read bulk-flips

  Privacy (2):
  19. notification payload contains no PII (no email, no nullifier)
  20. user cannot mark another user's notification (returns ok=False)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.models.schema import (
    Agent,
    Intent,
    Match,
    Notification,
    StepUpRequest,
    User,
)
from app.services import (
    deal_message_service,
    deal_service,
    embedding_service,
    match_service,
    negotiation_service,
    notification_service,
    step_up_service,
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


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_user(db, *, tier: int = 1, email: str | None = None) -> str:
    user_id = str(uuid.uuid4())
    email = email or f"u-{uuid.uuid4().hex[:8]}@x.com"
    user = User(id=user_id, **default_user_kwargs(tier=tier, email=email))
    db.add(user)
    await db.commit()
    return user_id


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


@dataclass
class FullDealSetup:
    seller_user_id: str
    seller_agent_id: str
    buyer_user_id: str
    buyer_agent_id: str
    sell_intent_id: str
    buy_intent_id: str
    match_id: str
    negotiation_id: str
    deal_id: str


async def _seed_pending_deal(db) -> FullDealSetup:
    seller_id, seller_agent, _ = await setup_active_mandate_async(
        db, email=f"sell-{uuid.uuid4().hex[:6]}@x.com"
    )
    buyer_id, buyer_agent, _ = await setup_active_mandate_async(
        db, email=f"buy-{uuid.uuid4().hex[:6]}@x.com"
    )
    sell_id = await _seed_intent(
        db, user_id=seller_id, side="sell", reservation_eur=1000, ideal_eur=1200
    )
    buy_id = await _seed_intent(
        db, user_id=buyer_id, side="buy", reservation_eur=1500, ideal_eur=1100
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
    return FullDealSetup(
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


@pytest.fixture
def webauthn_ok(monkeypatch):
    monkeypatch.setattr(
        "app.services.deal_service.verify_authentication_response",
        lambda **_: SimpleNamespace(new_sign_count=1),
    )
    monkeypatch.setattr(
        "app.services.step_up_service.verify_authentication_response",
        lambda **_: SimpleNamespace(new_sign_count=1),
    )


# ===========================================================================
# 1. create_notification persists
# ===========================================================================


@pytest.mark.db
async def test_create_notification_persists(async_db_session) -> None:
    user_id = await _seed_user(async_db_session, tier=0)
    n = await notification_service.create_notification(
        async_db_session,
        user_id=user_id,
        notification_type=notification_service.NotificationType.NEW_MATCH_DISCOVERED,
        title="Test",
        body="Body",
        payload={"foo": "bar"},
    )
    assert n is not None
    fetched = await async_db_session.get(Notification, n.id)
    assert fetched is not None
    assert fetched.type == "new_match_discovered"
    assert fetched.category == "match"
    assert fetched.payload == {"foo": "bar"}


# ===========================================================================
# 2. list filters unread_only
# ===========================================================================


@pytest.mark.db
async def test_list_filters_unread_only(async_db_session) -> None:
    user_id = await _seed_user(async_db_session, tier=0)
    a = await notification_service.create_notification(
        async_db_session,
        user_id=user_id,
        notification_type=notification_service.NotificationType.NEW_MATCH_DISCOVERED,
        title="A",
        body="b",
    )
    b = await notification_service.create_notification(
        async_db_session,
        user_id=user_id,
        notification_type=notification_service.NotificationType.OFFER_RECEIVED,
        title="B",
        body="b",
    )
    # Mark `a` as read.
    await notification_service.mark_read(
        async_db_session, user_id=user_id, notification_id=a.id
    )

    page = await notification_service.list_notifications(
        async_db_session, user_id=user_id, unread_only=True
    )
    ids = {r.id for r in page.rows}
    assert b.id in ids
    assert a.id not in ids


# ===========================================================================
# 3. list paginates via before_id cursor
# ===========================================================================


@pytest.mark.db
async def test_list_paginates_via_before_id(async_db_session) -> None:
    user_id = await _seed_user(async_db_session, tier=0)
    rows = []
    for i in range(5):
        r = await notification_service.create_notification(
            async_db_session,
            user_id=user_id,
            notification_type=notification_service.NotificationType.OFFER_RECEIVED,
            title=f"N{i}",
            body="b",
        )
        rows.append(r)
    # Newest is rows[-1]. Cursor before rows[-1] should yield rows[0..3].
    page = await notification_service.list_notifications(
        async_db_session,
        user_id=user_id,
        before_id=rows[-1].id,
        limit=10,
    )
    returned_ids = {r.id for r in page.rows}
    assert rows[-1].id not in returned_ids
    assert all(r.id in returned_ids for r in rows[:-1])


# ===========================================================================
# 4. mark_read idempotent
# ===========================================================================


@pytest.mark.db
async def test_mark_read_idempotent(async_db_session) -> None:
    user_id = await _seed_user(async_db_session, tier=0)
    n = await notification_service.create_notification(
        async_db_session,
        user_id=user_id,
        notification_type=notification_service.NotificationType.OFFER_RECEIVED,
        title="x",
        body="b",
    )
    first = await notification_service.mark_read(
        async_db_session, user_id=user_id, notification_id=n.id
    )
    second = await notification_service.mark_read(
        async_db_session, user_id=user_id, notification_id=n.id
    )
    assert first is True
    # Second call hits a read row → no rows updated.
    assert second is False


# ===========================================================================
# 5. cleanup_expired removes past-expiry rows
# ===========================================================================


@pytest.mark.db
async def test_cleanup_expired_removes_old(async_db_session) -> None:
    user_id = await _seed_user(async_db_session, tier=0)
    # Create one with explicit expired-in-past, one fresh.
    expired = await notification_service.create_notification(
        async_db_session,
        user_id=user_id,
        notification_type=notification_service.NotificationType.OFFER_RECEIVED,
        title="old",
        body="b",
        expires_at=datetime.utcnow() - timedelta(minutes=1),
    )
    fresh = await notification_service.create_notification(
        async_db_session,
        user_id=user_id,
        notification_type=notification_service.NotificationType.OFFER_RECEIVED,
        title="new",
        body="b",
    )
    deleted = await notification_service.cleanup_expired(async_db_session)
    assert deleted >= 1
    assert await async_db_session.get(Notification, expired.id) is None
    assert await async_db_session.get(Notification, fresh.id) is not None


# ===========================================================================
# 6 + 7. step_up sign / reject emit notifications
# ===========================================================================


async def _seed_pending_step_up(db, *, user_id: str) -> str:
    """Manually seed a StepUpRequest row (bypass sync tool_layer scaffold)."""
    import secrets

    user_id_, agent_id, mandate_id = await setup_active_mandate_async(
        db, email=f"step-{uuid.uuid4().hex[:6]}@x.com", user_id=user_id
    )
    req_id = str(uuid.uuid4())
    now = datetime.utcnow()
    req = StepUpRequest(
        id=req_id,
        agent_id=agent_id,
        mandate_id=mandate_id,
        user_id=user_id,
        action="accept_offer",
        action_params={"price_cents": 12000},
        reason="Price above threshold",
        challenge=secrets.token_bytes(32),
        canonical_payload=b'{"action":"accept_offer"}',
        status="pending",
        expires_at=now + timedelta(minutes=10),
        created_at=now,
    )
    db.add(req)
    await db.commit()
    return req_id


@pytest.mark.db
async def test_step_up_sign_creates_approved_notification(
    async_db_session, webauthn_ok
) -> None:
    user_id = str(uuid.uuid4())
    step_up_id = await _seed_pending_step_up(
        async_db_session, user_id=user_id
    )
    from app.services.mandate_service import WebAuthnAssertionPayload

    await step_up_service.sign(
        async_db_session,
        user_id=user_id,
        step_up_id=step_up_id,
        assertion=WebAuthnAssertionPayload(**_fake_assertion()),
    )
    rows = list(
        await async_db_session.scalars(
            select(Notification)
            .where(Notification.user_id == user_id)
            .where(Notification.type == "step_up_approved")
        )
    )
    assert len(rows) == 1


@pytest.mark.db
async def test_step_up_reject_creates_rejected_notification(
    async_db_session,
) -> None:
    user_id = str(uuid.uuid4())
    step_up_id = await _seed_pending_step_up(
        async_db_session, user_id=user_id
    )
    await step_up_service.reject(
        async_db_session, user_id=user_id, step_up_id=step_up_id
    )
    rows = list(
        await async_db_session.scalars(
            select(Notification)
            .where(Notification.user_id == user_id)
            .where(Notification.type == "step_up_rejected")
        )
    )
    assert len(rows) == 1


# ===========================================================================
# 8. match discovery emits NEW_MATCH for both parties
# ===========================================================================


@pytest.mark.db
async def test_match_discovery_notifies_both_parties(async_db_session) -> None:
    a = await _seed_user(async_db_session, tier=1, email="a@x.com")
    b = await _seed_user(async_db_session, tier=1, email="b@x.com")
    sell_id = await _seed_intent(async_db_session, user_id=a, side="sell")
    await _seed_intent(
        async_db_session,
        user_id=b,
        side="buy",
        reservation_eur=1500,
        ideal_eur=1100,
    )
    await match_service.find_matches_for_intent(
        async_db_session, intent_id=sell_id
    )

    rows_a = list(
        await async_db_session.scalars(
            select(Notification)
            .where(Notification.user_id == a)
            .where(Notification.type == "new_match_discovered")
        )
    )
    rows_b = list(
        await async_db_session.scalars(
            select(Notification)
            .where(Notification.user_id == b)
            .where(Notification.type == "new_match_discovered")
        )
    )
    assert len(rows_a) == 1
    assert len(rows_b) == 1


# ===========================================================================
# 9. start_or_continue emits OFFER_RECEIVED for counterparty only
# ===========================================================================


@pytest.mark.db
async def test_offer_notifies_counterparty_only(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    # The seller offered first in _seed_pending_deal; the OFFER_RECEIVED
    # notification went to the buyer, not the seller.
    seller_offers = list(
        await async_db_session.scalars(
            select(Notification)
            .where(Notification.user_id == s.seller_user_id)
            .where(Notification.type == "offer_received")
        )
    )
    buyer_offers = list(
        await async_db_session.scalars(
            select(Notification)
            .where(Notification.user_id == s.buyer_user_id)
            .where(Notification.type == "offer_received")
        )
    )
    assert seller_offers == []
    assert len(buyer_offers) == 1


# ===========================================================================
# 10. accept emits DEAL_AWAITING_YOUR_SIGNATURE for both
# ===========================================================================


@pytest.mark.db
async def test_accept_notifies_both_for_signing(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    for user_id in (s.buyer_user_id, s.seller_user_id):
        rows = list(
            await async_db_session.scalars(
                select(Notification)
                .where(Notification.user_id == user_id)
                .where(Notification.type == "deal_awaiting_your_signature")
            )
        )
        assert len(rows) == 1


# ===========================================================================
# 11. submit_signature first sig emits OTHER_PARTY_SIGNED
# ===========================================================================


@pytest.mark.db
async def test_first_signature_notifies_other_party(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
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

    rows_seller = list(
        await async_db_session.scalars(
            select(Notification)
            .where(Notification.user_id == s.seller_user_id)
            .where(Notification.type == "deal_other_party_signed")
        )
    )
    assert len(rows_seller) == 1


# ===========================================================================
# 12. dual signature emits DEAL_CONFIRMED for both
# ===========================================================================


@pytest.mark.db
async def test_dual_signature_notifies_both_with_confirmed(
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

    for user_id in (s.buyer_user_id, s.seller_user_id):
        rows = list(
            await async_db_session.scalars(
                select(Notification)
                .where(Notification.user_id == user_id)
                .where(Notification.type == "deal_confirmed")
            )
        )
        assert len(rows) == 1


# ===========================================================================
# 13. chat message notifies recipient
# ===========================================================================


@pytest.mark.db
async def test_chat_message_notifies_recipient(
    async_db_session, webauthn_ok
) -> None:
    s = await _seed_pending_deal(async_db_session)
    from app.services.mandate_service import WebAuthnAssertionPayload

    # Confirm deal first (chat is gated to confirmed deals).
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

    await deal_message_service.send_message(
        async_db_session,
        user_id=s.buyer_user_id,
        deal_id=s.deal_id,
        encrypted_content=b"hello",
        nonce=b"x" * 12,
    )
    rows_seller = list(
        await async_db_session.scalars(
            select(Notification)
            .where(Notification.user_id == s.seller_user_id)
            .where(Notification.type == "deal_message_received")
        )
    )
    rows_buyer_self = list(
        await async_db_session.scalars(
            select(Notification)
            .where(Notification.user_id == s.buyer_user_id)
            .where(Notification.type == "deal_message_received")
        )
    )
    assert len(rows_seller) == 1
    # Sender does NOT get notified about their own message.
    assert rows_buyer_self == []


# ===========================================================================
# 14. GET /api/notifications returns caller's only
# ===========================================================================


@pytest.mark.db
async def test_get_notifications_only_for_caller(
    http_client, async_db_session
) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a14@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b14@x.com")
    await notification_service.create_notification(
        async_db_session,
        user_id=a,
        notification_type=notification_service.NotificationType.OFFER_RECEIVED,
        title="A's",
        body="b",
    )
    await notification_service.create_notification(
        async_db_session,
        user_id=b,
        notification_type=notification_service.NotificationType.OFFER_RECEIVED,
        title="B's",
        body="b",
    )

    _bearer(http_client, a, tier=0)
    response = await http_client.get("/api/notifications")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["notifications"][0]["title"] == "A's"


# ===========================================================================
# 15. GET /api/notifications/unread-count
# ===========================================================================


@pytest.mark.db
async def test_unread_count_endpoint(http_client, async_db_session) -> None:
    user_id = await _seed_user(async_db_session, tier=0)
    for _ in range(3):
        await notification_service.create_notification(
            async_db_session,
            user_id=user_id,
            notification_type=notification_service.NotificationType.OFFER_RECEIVED,
            title="x",
            body="b",
        )
    _bearer(http_client, user_id, tier=0)
    response = await http_client.get("/api/notifications/unread-count")
    assert response.status_code == 200
    assert response.json()["unread_count"] == 3


# ===========================================================================
# 16. POST .../read flips read_at
# ===========================================================================


@pytest.mark.db
async def test_mark_read_endpoint(http_client, async_db_session) -> None:
    user_id = await _seed_user(async_db_session, tier=0)
    n = await notification_service.create_notification(
        async_db_session,
        user_id=user_id,
        notification_type=notification_service.NotificationType.OFFER_RECEIVED,
        title="x",
        body="b",
    )
    _bearer(http_client, user_id, tier=0)
    r = await http_client.post(f"/api/notifications/{n.id}/read")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    fetched = await async_db_session.get(Notification, n.id)
    await async_db_session.refresh(fetched)
    assert fetched.read_at is not None


# ===========================================================================
# 17. POST .../acted flips both
# ===========================================================================


@pytest.mark.db
async def test_mark_acted_endpoint(http_client, async_db_session) -> None:
    user_id = await _seed_user(async_db_session, tier=0)
    n = await notification_service.create_notification(
        async_db_session,
        user_id=user_id,
        notification_type=notification_service.NotificationType.STEP_UP_REQUIRED,
        title="x",
        body="b",
    )
    _bearer(http_client, user_id, tier=0)
    r = await http_client.post(f"/api/notifications/{n.id}/acted")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    fetched = await async_db_session.get(Notification, n.id)
    await async_db_session.refresh(fetched)
    assert fetched.read_at is not None
    assert fetched.acted_at is not None


# ===========================================================================
# 18. POST mark-all-read bulk
# ===========================================================================


@pytest.mark.db
async def test_mark_all_read_endpoint(http_client, async_db_session) -> None:
    user_id = await _seed_user(async_db_session, tier=0)
    for _ in range(3):
        await notification_service.create_notification(
            async_db_session,
            user_id=user_id,
            notification_type=notification_service.NotificationType.OFFER_RECEIVED,
            title="x",
            body="b",
        )
    _bearer(http_client, user_id, tier=0)
    r = await http_client.post("/api/notifications/mark-all-read")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["marked_count"] == 3
    assert (
        await notification_service.unread_count(
            async_db_session, user_id=user_id
        )
        == 0
    )


# ===========================================================================
# 19. notification payload contains no PII
# ===========================================================================


@pytest.mark.db
async def test_notification_payload_no_pii(async_db_session) -> None:
    s = await _seed_pending_deal(async_db_session)
    # Inspect the deal_awaiting_your_signature payload that was created
    # by accept_offer. Verify it has no email / nullifier_hash leakage.
    rows = list(
        await async_db_session.scalars(
            select(Notification)
            .where(Notification.type == "deal_awaiting_your_signature")
        )
    )
    assert rows
    for row in rows:
        payload_str = str(row.payload).lower()
        assert "@" not in payload_str  # no email
        assert "nullifier" not in payload_str
        assert "passkey" not in payload_str


# ===========================================================================
# 20. user cannot mark another user's notification (returns ok=False)
# ===========================================================================


@pytest.mark.db
async def test_user_cannot_mark_others_notification(
    http_client, async_db_session
) -> None:
    a = await _seed_user(async_db_session, tier=0, email="a20@x.com")
    b = await _seed_user(async_db_session, tier=0, email="b20@x.com")
    n = await notification_service.create_notification(
        async_db_session,
        user_id=a,
        notification_type=notification_service.NotificationType.OFFER_RECEIVED,
        title="A's",
        body="b",
    )
    # B tries to mark A's notification.
    _bearer(http_client, b, tier=0)
    r = await http_client.post(f"/api/notifications/{n.id}/read")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    # Original row is still unread.
    fetched = await async_db_session.get(Notification, n.id)
    await async_db_session.refresh(fetched)
    assert fetched.read_at is None
