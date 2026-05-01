"""Content moderation — service + API integration (brief task 7.1.4).

8 tests verify that user-generated text fields hitting the API surface
(intent create/update + negotiation start/reject) are moderated by the
service layer, and that the global `ModerationError` handler returns
the canonical `{detail: {code, message, field}}` envelope at HTTP 422.

Tests skip the full happy-path (the user JWT in `authenticated_client`
doesn't seed a DB user, so create flows naturally 404 after moderation
passes — that's enough to verify "moderation didn't fire" by asserting
the error code is NOT a moderation one).
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest


_VALID_CREATE_BODY: dict[str, Any] = {
    "side": "buy",
    "title": "Vintage denim jacket",
    "description": "Looking for size M",
    "category": "fashion_clothing",
    "reservation_price_eur": 80.0,
    "ideal_price_eur": 60.0,
}


_MODERATION_CODES = {"empty_after_strip", "too_long", "profanity_detected"}


def _detail_code(response) -> str:
    """Extract `detail.code` from the response. Tolerates flat `code`
    shape too in case a non-moderation error path returns the rate-limit-
    style envelope."""
    body = response.json()
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and "code" in detail:
        return detail["code"]
    return body.get("code", "")


# ---------------------------------------------------------------------------
# POST /api/intents — create flow
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_create_intent_rejects_empty_title_after_strip(
    authenticated_client,
):
    client, _ = authenticated_client(tier=2)
    body = {**_VALID_CREATE_BODY, "title": "   "}
    r = await client.post("/api/intents", json=body)
    assert r.status_code == 422, r.text
    assert _detail_code(r) == "empty_after_strip"
    assert r.json()["detail"]["field"] == "title"


@pytest.mark.db
async def test_create_intent_rejects_title_over_max_length(
    authenticated_client,
):
    client, _ = authenticated_client(tier=2)
    body = {**_VALID_CREATE_BODY, "title": "x" * 201}
    r = await client.post("/api/intents", json=body)
    assert r.status_code == 422, r.text
    assert _detail_code(r) == "too_long"
    assert r.json()["detail"]["field"] == "title"


@pytest.mark.db
async def test_create_intent_rejects_profanity_in_title(authenticated_client):
    client, _ = authenticated_client(tier=2)
    body = {**_VALID_CREATE_BODY, "title": "fuck this listing"}
    r = await client.post("/api/intents", json=body)
    assert r.status_code == 422, r.text
    assert _detail_code(r) == "profanity_detected"
    assert r.json()["detail"]["field"] == "title"


@pytest.mark.db
async def test_create_intent_rejects_profanity_in_description(
    authenticated_client,
):
    client, _ = authenticated_client(tier=2)
    body = {**_VALID_CREATE_BODY, "description": "this is shit quality"}
    r = await client.post("/api/intents", json=body)
    assert r.status_code == 422, r.text
    assert _detail_code(r) == "profanity_detected"
    assert r.json()["detail"]["field"] == "description"


@pytest.mark.db
async def test_create_intent_clean_text_passes_moderation(
    authenticated_client,
):
    """Clean title + description must NOT 422 with a moderation code.

    The downstream `user_not_found` 404 is expected (authenticated_client
    doesn't seed a DB user) and confirms the request reached past the
    moderation phase."""
    client, _ = authenticated_client(tier=2)
    r = await client.post("/api/intents", json=_VALID_CREATE_BODY)
    assert _detail_code(r) not in _MODERATION_CODES, r.text


# ---------------------------------------------------------------------------
# POST /api/negotiations — start flow
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_start_negotiation_rejects_profanity_in_message(
    authenticated_client,
):
    client, _ = authenticated_client(tier=1)
    body = {
        "match_id": str(uuid.uuid4()),
        "agent_id": str(uuid.uuid4()),
        "price_cents": 5000,
        "message": "vaffanculo",
    }
    r = await client.post("/api/negotiations", json=body)
    assert r.status_code == 422, r.text
    assert _detail_code(r) == "profanity_detected"
    assert r.json()["detail"]["field"] == "message"


# ---------------------------------------------------------------------------
# PATCH /api/intents/{id} — update flow + moderate_optional null skip
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_update_intent_rejects_profanity_in_title(authenticated_client):
    client, _ = authenticated_client(tier=2)
    intent_id = str(uuid.uuid4())
    r = await client.patch(
        f"/api/intents/{intent_id}", json={"title": "fuck this"}
    )
    assert r.status_code == 422, r.text
    assert _detail_code(r) == "profanity_detected"
    assert r.json()["detail"]["field"] == "title"


@pytest.mark.db
async def test_update_intent_with_null_description_skips_moderation(
    authenticated_client,
):
    """`moderate_optional` returns immediately on `None` — verified by
    asserting the request advances past the moderation phase. The
    downstream `intent_not_found` 404 (random UUID) confirms moderation
    didn't short-circuit on the `None` description."""
    client, _ = authenticated_client(tier=2)
    intent_id = str(uuid.uuid4())
    r = await client.patch(
        f"/api/intents/{intent_id}",
        json={"title": "Clean update", "description": None},
    )
    assert _detail_code(r) not in _MODERATION_CODES, r.text
