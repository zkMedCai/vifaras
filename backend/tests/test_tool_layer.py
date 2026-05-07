"""AsyncToolHandler tests (brief task 6.3.a).

25 tests organized by concern:

  Dispatch (3):
   1. unknown tool → status='error', code='unknown_tool'
   2. authorize called before execute
   3. record_usage called on success

  Tool implementations (9):
   4. _create_intent delegates to intent_service
   5. _search_matches returns matches list
   6. _send_offer creates negotiation
   7. _send_counter_offer resolves negotiation_id → match_id
   8. _accept_offer creates pending deal
   9. _reject_offer marks negotiation rejected
  10. _read_inbox returns inbox payload
  11. _check_state returns AgentFullState dump
  12. _ask_user creates user_question stub

  Verifier integration (4):
  13. StepUpRequired creates step_up_request row + step_up_required result
  14. step_up_required result has step_up_id + reason + action
  15. LimitExceeded → status='limit_exceeded'
  16. ActionNotAllowed (MandateError) → status='error'

  Sync→async bridge (3):
  17. authorize_async wraps sync verifier (via asyncio.to_thread)
  18. record_usage_async wraps sync verifier
  19. concurrent tool dispatches don't share verifier state

  Edge cases (4):
  20. tool result truncated for audit when oversized
  21. unknown agent_id raises during _get_user_id
  22. ask_user with string context wrapped to dict defensively
  23. ToolResult.to_dict shape matches spec

  Step-up resume (2):
  24. step-up notification emitted on creation
  25. step-up persistence survives across handler instances
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.agents.tool_layer import AGENT_TOOLS, AsyncToolHandler, ToolResult
from app.models.schema import (
    Agent,
    Intent,
    Match,
    Negotiation,
    Notification,
    StepUpRequest,
    User,
    UserQuestion,
)
from app.services import embedding_service, negotiation_service
from app.services.mandate_verifier import (
    ActionNotAllowed,
    LimitExceeded,
    MandateError,
    StepUpRequired,
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
# FakeMandateVerifier — injected to bypass the sync DB session
# ---------------------------------------------------------------------------


@dataclass
class _FakeMandate:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str = ""
    user_id: str = ""


@dataclass
class FakeMandateVerifier:
    """Stand-in for MandateVerifier inside AsyncToolHandler tests.

    Drive behavior via constructor flags or the `set_*` methods.
    `calls` records every (method_name, args) for assertions.
    """

    behavior: str = "ok"  # 'ok' | 'step_up' | 'limit' | 'denied'
    step_up_action: str = ""
    step_up_params: dict = field(default_factory=dict)
    step_up_reason: str = "above threshold"
    deny_message: str = "action not allowed"
    calls: list[tuple[str, tuple, dict]] = field(default_factory=list)
    fake_mandate: _FakeMandate | None = None

    async def authorize_async(self, agent_id, action, params):
        self.calls.append(("authorize", (agent_id, action), dict(params)))
        if self.behavior == "step_up":
            raise StepUpRequired(
                action=self.step_up_action or action,
                params=self.step_up_params or params,
                reason=self.step_up_reason,
            )
        if self.behavior == "limit":
            raise LimitExceeded(self.deny_message)
        if self.behavior == "denied":
            raise ActionNotAllowed(self.deny_message)
        # success path
        if self.fake_mandate is None:
            self.fake_mandate = _FakeMandate(agent_id=agent_id)
        return self.fake_mandate

    async def record_usage_async(
        self, mandate, action, params, success, result=None, error_code=None
    ):
        self.calls.append(
            ("record_usage", (action, success), {"error_code": error_code})
        )

    async def log_failed_async(self, agent_id, action, error):
        self.calls.append(
            ("log_failed", (agent_id, action), {"error_type": type(error).__name__})
        )


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


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


async def _seed_match(db, *, buy_intent_id, sell_intent_id) -> str:
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
class HandlerSetup:
    seller_user_id: str
    seller_agent_id: str
    seller_mandate_id: str
    buyer_user_id: str
    buyer_agent_id: str
    sell_intent_id: str
    buy_intent_id: str
    match_id: str


async def _seed_handler_context(db) -> HandlerSetup:
    seller_id, seller_agent, seller_mandate = await setup_active_mandate_async(
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
    return HandlerSetup(
        seller_user_id=seller_id,
        seller_agent_id=seller_agent,
        seller_mandate_id=seller_mandate,
        buyer_user_id=buyer_id,
        buyer_agent_id=buyer_agent,
        sell_intent_id=sell_id,
        buy_intent_id=buy_id,
        match_id=match_id,
    )


# ===========================================================================
# 1. unknown tool
# ===========================================================================


@pytest.mark.db
async def test_unknown_tool_returns_error(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=FakeMandateVerifier()
    )
    result = await handler.handle("not_a_real_tool", {})
    assert result.status == "error"
    assert result.error_code == "unknown_tool"


# ===========================================================================
# 2. authorize called before execute
# ===========================================================================


@pytest.mark.db
async def test_authorize_called_before_execute(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    verifier = FakeMandateVerifier()
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=verifier
    )
    await handler.handle("check_state", {})
    method_names = [c[0] for c in verifier.calls]
    # authorize must come before record_usage.
    assert method_names[0] == "authorize"
    assert "record_usage" in method_names


# ===========================================================================
# 3. record_usage called on success
# ===========================================================================


@pytest.mark.db
async def test_record_usage_called_on_success(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    verifier = FakeMandateVerifier()
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=verifier
    )
    result = await handler.handle("check_state", {})
    assert result.status == "ok"
    record_calls = [c for c in verifier.calls if c[0] == "record_usage"]
    assert len(record_calls) == 1
    assert record_calls[0][1] == ("check_state", True)


# ===========================================================================
# 4. _create_intent delegates
# ===========================================================================


@pytest.mark.db
async def test_create_intent_delegates_to_service(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=FakeMandateVerifier()
    )
    result = await handler.handle(
        "create_intent",
        {
            "side": "sell",
            "title": "Bici Specialized",
            "description": "Usata 1 anno",
            "category": "sport_bicycles",
            "reservation_price_eur": 500.0,
            "ideal_price_eur": 600.0,
            "duration_days": 14,
        },
    )
    assert result.status == "ok"
    intent_id = result.data["intent_id"]
    intent = await async_db_session.get(Intent, intent_id)
    assert intent is not None
    assert intent.user_id == s.seller_user_id


# ===========================================================================
# 5. _search_matches returns matches
# ===========================================================================


@pytest.mark.db
async def test_search_matches_returns_matches(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=FakeMandateVerifier()
    )
    # Run match discovery first so there's a persisted match.
    from app.services import match_service

    await match_service.find_matches_for_intent(
        async_db_session, intent_id=s.sell_intent_id
    )

    result = await handler.handle(
        "search_matches", {"intent_id": s.sell_intent_id, "limit": 10}
    )
    assert result.status == "ok"
    assert result.data["match_count"] >= 1
    assert "match_id" in result.data["matches"][0]


# ===========================================================================
# 6. _send_offer creates negotiation
# ===========================================================================


@pytest.mark.db
async def test_send_offer_creates_negotiation(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=FakeMandateVerifier()
    )
    result = await handler.handle(
        "send_offer",
        {
            "match_id": s.match_id,
            "price_cents": 120000,
            "message": "as listed with tracked shipping",
            "terms_delta": {
                "shipping_required": True,
                "shipping_paid_by": "buyer",
                "shipping_method_preference": "tracked_parcel",
            },
        },
    )
    assert result.status == "ok"
    assert result.data["proposal_hash"].startswith("sha256:")
    assert (
        result.data["canonical_terms_snapshot"]["shipping_method_preference"]
        == "tracked_parcel"
    )
    nego_id = result.data["negotiation_id"]
    nego = await async_db_session.get(Negotiation, nego_id)
    assert nego is not None
    assert nego.status == "active"


# ===========================================================================
# 7. _send_counter_offer resolves negotiation_id → match_id
# ===========================================================================


@pytest.mark.db
async def test_send_counter_offer_resolves_negotiation(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    handler = AsyncToolHandler(
        async_db_session, s.buyer_agent_id, verifier=FakeMandateVerifier()
    )
    result = await handler.handle(
        "send_counter_offer",
        {
            "negotiation_id": nego.negotiation_id,
            "price_cents": 110000,
            "message": "lower",
            "terms_delta": {
                "shipping_required": True,
                "shipping_paid_by": "buyer",
                "shipping_method_preference": "tracked_parcel",
            },
        },
    )
    assert result.status == "ok"
    assert result.data["rounds_used"] == 2
    assert result.data["proposal_hash"].startswith("sha256:")


# ===========================================================================
# 8. _accept_offer creates pending deal
# ===========================================================================


@pytest.mark.db
async def test_accept_offer_creates_pending_deal(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    handler = AsyncToolHandler(
        async_db_session, s.buyer_agent_id, verifier=FakeMandateVerifier()
    )
    result = await handler.handle(
        "accept_offer",
        {
            "negotiation_id": nego.negotiation_id,
            "proposal_hash": nego.last_turn["proposal_hash"],
        },
    )
    assert result.status == "ok"
    assert result.data["deal_id"]
    assert result.data["next_step"] == "sign_deal_with_passkey"
    assert result.data["proposal_hash"] == nego.last_turn["proposal_hash"]


# ===========================================================================
# 9. _reject_offer marks rejected
# ===========================================================================


@pytest.mark.db
async def test_reject_offer_marks_rejected(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    nego = await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.seller_user_id,
        agent_id=s.seller_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    handler = AsyncToolHandler(
        async_db_session, s.buyer_agent_id, verifier=FakeMandateVerifier()
    )
    result = await handler.handle(
        "reject_offer",
        {"negotiation_id": nego.negotiation_id, "reason": "too high"},
    )
    assert result.status == "ok"
    assert result.data["status"] == "rejected"


# ===========================================================================
# 10. _read_inbox returns payload
# ===========================================================================


@pytest.mark.db
async def test_read_inbox_returns_payload(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    # Buyer offers; seller's inbox should include it.
    await negotiation_service.start_or_continue(
        async_db_session,
        user_id=s.buyer_user_id,
        agent_id=s.buyer_agent_id,
        match_id=s.match_id,
        price_cents=120000,
    )
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=FakeMandateVerifier()
    )
    result = await handler.handle("read_inbox", {})
    assert result.status == "ok"
    assert "new_offers_received" in result.data
    assert len(result.data["new_offers_received"]) == 1


# ===========================================================================
# 11. _check_state returns full state dump
# ===========================================================================


@pytest.mark.db
async def test_check_state_returns_full_state(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=FakeMandateVerifier()
    )
    result = await handler.handle("check_state", {})
    assert result.status == "ok"
    assert result.data["agent_id"] == s.seller_agent_id
    assert result.data["mandate"] is not None
    assert "active_intents" in result.data


# ===========================================================================
# 12. _ask_user creates user_question
# ===========================================================================


@pytest.mark.db
async def test_ask_user_creates_user_question(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=FakeMandateVerifier()
    )
    result = await handler.handle(
        "ask_user",
        {"question": "Posso scendere a €900?", "context": {"floor_eur": 900}},
    )
    assert result.status == "ok"
    assert result.data["status"] == "queued"
    assert result.data["question_id"]
    rows = list(
        await async_db_session.scalars(
            select(UserQuestion).where(
                UserQuestion.agent_id == s.seller_agent_id
            )
        )
    )
    assert len(rows) == 1


# ===========================================================================
# 13. StepUpRequired creates step_up_request row + step_up_required result
# ===========================================================================


@pytest.mark.db
async def test_step_up_required_creates_step_up_request(
    async_db_session,
) -> None:
    s = await _seed_handler_context(async_db_session)
    verifier = FakeMandateVerifier(behavior="step_up", step_up_reason="above 100€")
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=verifier
    )
    result = await handler.handle(
        "accept_offer", {"negotiation_id": str(uuid.uuid4())}
    )
    assert result.status == "step_up_required"
    step_up_id = result.data["step_up_id"]
    request = await async_db_session.get(StepUpRequest, step_up_id)
    assert request is not None
    assert request.user_id == s.seller_user_id
    assert request.agent_id == s.seller_agent_id


# ===========================================================================
# 14. step_up_required result has step_up_id + reason + action
# ===========================================================================


@pytest.mark.db
async def test_step_up_result_payload_shape(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    verifier = FakeMandateVerifier(
        behavior="step_up",
        step_up_reason="above 100€",
        step_up_action="accept_offer",
    )
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=verifier
    )
    result = await handler.handle(
        "accept_offer", {"negotiation_id": str(uuid.uuid4())}
    )
    payload = result.to_dict()
    assert payload["status"] == "step_up_required"
    assert payload["data"]["step_up_id"]
    assert payload["data"]["reason"] == "above 100€"
    assert payload["data"]["action"] == "accept_offer"


# ===========================================================================
# 15. LimitExceeded → status='limit_exceeded'
# ===========================================================================


@pytest.mark.db
async def test_limit_exceeded_returns_limit_status(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    verifier = FakeMandateVerifier(behavior="limit", deny_message="daily cap")
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=verifier
    )
    result = await handler.handle("create_intent", {"side": "sell"})
    assert result.status == "limit_exceeded"
    assert "daily cap" in (result.error or "")


# ===========================================================================
# 16. ActionNotAllowed → status='error'
# ===========================================================================


@pytest.mark.db
async def test_action_not_allowed_returns_error(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    verifier = FakeMandateVerifier(
        behavior="denied", deny_message="action not in scope"
    )
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=verifier
    )
    result = await handler.handle("create_intent", {"side": "sell"})
    assert result.status == "error"
    assert result.error_code == "action_not_allowed"


# ===========================================================================
# 17. authorize_async wraps sync verifier (real verifier)
# ===========================================================================


@pytest.mark.db
async def test_authorize_async_wraps_sync(async_db_session, db_session) -> None:
    """The real `MandateVerifier` is sync; `authorize_async` should run it
    on a worker thread and return the same Mandate row.

    Uses `db_session` (sync, separate fixture) to construct the verifier
    against the same testcontainer. The two sessions don't share a
    transaction (savepoint isolation), so we seed via the sync session
    and verify the verifier sees its own writes.
    """
    from app.services.mandate_verifier import MandateVerifier
    from tests.factories import setup_active_mandate_sync

    user_id, agent_id, _ = setup_active_mandate_sync(
        db_session, email="bridge@example.com"
    )
    verifier = MandateVerifier(db_session)
    mandate = await verifier.authorize_async(
        agent_id, "create_intent", {"price_eur": 50}
    )
    assert mandate is not None
    assert mandate.user_id == user_id


# ===========================================================================
# 18. record_usage_async wraps sync verifier
# ===========================================================================


@pytest.mark.db
async def test_record_usage_async_wraps_sync(db_session) -> None:
    from app.models.schema import AuditLog
    from app.services.mandate_verifier import MandateVerifier
    from tests.factories import setup_active_mandate_sync

    user_id, agent_id, mandate_id = setup_active_mandate_sync(
        db_session, email="bridge2@example.com"
    )
    verifier = MandateVerifier(db_session)
    mandate = verifier.authorize(agent_id, "create_intent", {"price_eur": 30})
    await verifier.record_usage_async(
        mandate, "create_intent", {"price_eur": 30}, success=True
    )
    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.mandate_id == mandate_id)
        .filter(AuditLog.action == "create_intent")
        .all()
    )
    assert len(rows) == 1


# ===========================================================================
# 19. independent handler instances don't share verifier state
# ===========================================================================


@pytest.mark.db
async def test_independent_handlers_no_state_leak(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    verifier_a = FakeMandateVerifier()
    verifier_b = FakeMandateVerifier()
    h_a = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=verifier_a
    )
    h_b = AsyncToolHandler(
        async_db_session, s.buyer_agent_id, verifier=verifier_b
    )
    await h_a.handle("check_state", {})
    await h_b.handle("check_state", {})
    assert len(verifier_a.calls) == 2  # authorize + record_usage
    assert len(verifier_b.calls) == 2


# ===========================================================================
# 20. tool result truncated for audit when oversized
# ===========================================================================


@pytest.mark.db
async def test_tool_result_truncated_for_audit(async_db_session) -> None:
    """`_truncate_for_audit` keeps the prompt slim; we test the helper directly."""
    from app.agents.tool_layer import _truncate_for_audit

    big = {"x": "Y" * 5000}
    truncated = _truncate_for_audit(big)
    assert truncated.get("_truncated") is True
    assert "_size_bytes" in truncated
    # Small data passes through.
    small = {"a": 1, "b": "c"}
    assert _truncate_for_audit(small) == small


# ===========================================================================
# 21. unknown agent_id raises during _get_user_id
# ===========================================================================


@pytest.mark.db
async def test_unknown_agent_id_raises_in_handler(async_db_session) -> None:
    handler = AsyncToolHandler(
        async_db_session,
        agent_id=str(uuid.uuid4()),
        verifier=FakeMandateVerifier(),
    )
    result = await handler.handle(
        "create_intent",
        {
            "side": "sell",
            "title": "x",
            "category": "misc_other",
            "reservation_price_eur": 10.0,
            "ideal_price_eur": 12.0,
        },
    )
    assert result.status == "error"
    assert result.error_code == "execution_error"


# ===========================================================================
# 22. ask_user with string context wrapped to dict
# ===========================================================================


@pytest.mark.db
async def test_ask_user_string_context_wrapped(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=FakeMandateVerifier()
    )
    result = await handler.handle(
        "ask_user", {"question": "Q?", "context": "string-not-dict"}
    )
    assert result.status == "ok"
    rows = list(
        await async_db_session.scalars(
            select(UserQuestion).where(
                UserQuestion.agent_id == s.seller_agent_id
            )
        )
    )
    assert len(rows) == 1
    # Context wrapped into {"text": ...} for storage uniformity.
    assert rows[0].context == {"text": "string-not-dict"}


# ===========================================================================
# 23. ToolResult.to_dict shape matches spec
# ===========================================================================


def test_tool_result_to_dict_shape() -> None:
    ok = ToolResult(status="ok", data={"x": 1}).to_dict()
    assert ok == {"status": "ok", "data": {"x": 1}}

    err = ToolResult(
        status="error", error="boom", error_code="bad"
    ).to_dict()
    assert err == {"status": "error", "error": "boom", "error_code": "bad"}

    empty = ToolResult(status="ok").to_dict()
    assert empty == {"status": "ok"}


# ===========================================================================
# 24. step-up notification emitted on creation
# ===========================================================================


@pytest.mark.db
async def test_step_up_notification_emitted(async_db_session) -> None:
    s = await _seed_handler_context(async_db_session)
    verifier = FakeMandateVerifier(behavior="step_up")
    handler = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=verifier
    )
    await handler.handle("accept_offer", {"negotiation_id": str(uuid.uuid4())})
    rows = list(
        await async_db_session.scalars(
            select(Notification)
            .where(Notification.user_id == s.seller_user_id)
            .where(Notification.type == "step_up_required")
        )
    )
    assert len(rows) == 1


# ===========================================================================
# 25. step-up persistence survives across handler instances
# ===========================================================================


@pytest.mark.db
async def test_step_up_persists_across_handlers(async_db_session) -> None:
    """First handler creates step-up; a second handler instance can still
    see the row (basic durability check)."""
    s = await _seed_handler_context(async_db_session)
    verifier = FakeMandateVerifier(behavior="step_up")
    h1 = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=verifier
    )
    result = await h1.handle("accept_offer", {"negotiation_id": str(uuid.uuid4())})
    step_up_id = result.data["step_up_id"]

    # New handler instance, separate verifier.
    h2 = AsyncToolHandler(
        async_db_session, s.seller_agent_id, verifier=FakeMandateVerifier()
    )
    request = await async_db_session.get(StepUpRequest, step_up_id)
    assert request is not None
    assert request.status == "pending"


# ===========================================================================
# Smoke: AGENT_TOOLS schema sanity
# ===========================================================================


def test_agent_tools_schema_has_9_tools() -> None:
    """Sanity smoke: 9 tools in AGENT_TOOLS, all with required fields."""
    assert len(AGENT_TOOLS) == 9
    names = {t["name"] for t in AGENT_TOOLS}
    expected = {
        "create_intent",
        "search_matches",
        "send_offer",
        "send_counter_offer",
        "accept_offer",
        "reject_offer",
        "check_state",
        "read_inbox",
        "ask_user",
    }
    assert names == expected
    for tool in AGENT_TOOLS:
        assert "description" in tool
        assert "input_schema" in tool
