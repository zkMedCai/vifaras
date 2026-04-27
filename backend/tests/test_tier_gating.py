"""Tier-based gating middleware tests (brief task 2.2).

Exercises `core/security.require_tier(N)` against the dev-only endpoints
`/api/_test/tier{0,1,2}`. Uses the `authenticated_client(tier=N)` factory
fixture which mints a JWT directly — no DB user needed because the gating
middleware decodes the JWT only.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# tier=0 user
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_tier_0_user_passes_tier_0_endpoint(authenticated_client) -> None:
    client, ctx = authenticated_client(tier=0)
    resp = await client.get("/api/_test/tier0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["tier"] == 0
    assert body["user_id"] == ctx["user_id"]


@pytest.mark.db
async def test_tier_0_user_blocked_from_tier_1_with_402_and_next_step(
    authenticated_client,
) -> None:
    client, _ = authenticated_client(tier=0)
    resp = await client.get("/api/_test/tier1")
    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert detail["code"] == "tier_upgrade_required"
    assert detail["required_tier"] == 1
    assert detail["current_tier"] == 0
    assert detail["next_step"]["path"] == "/api/identity/verify-self"
    assert detail["next_step"]["description"]  # non-empty copy


@pytest.mark.db
async def test_tier_0_user_blocked_from_tier_2(authenticated_client) -> None:
    client, _ = authenticated_client(tier=0)
    resp = await client.get("/api/_test/tier2")
    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert detail["required_tier"] == 2
    assert detail["current_tier"] == 0


# ---------------------------------------------------------------------------
# tier=1 user
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_tier_1_user_passes_tier_0_and_tier_1(authenticated_client) -> None:
    client, _ = authenticated_client(tier=1)
    assert (await client.get("/api/_test/tier0")).status_code == 200
    assert (await client.get("/api/_test/tier1")).status_code == 200


@pytest.mark.db
async def test_tier_1_user_blocked_from_tier_2_with_mandate_next_step(
    authenticated_client,
) -> None:
    client, _ = authenticated_client(tier=1)
    resp = await client.get("/api/_test/tier2")
    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert detail["next_step"]["path"] == "/api/mandates/draft"


# ---------------------------------------------------------------------------
# tier=2 user
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_tier_2_user_passes_all_three_endpoints(authenticated_client) -> None:
    client, _ = authenticated_client(tier=2)
    for path in ("/api/_test/tier0", "/api/_test/tier1", "/api/_test/tier2"):
        resp = await client.get(path)
        assert resp.status_code == 200, f"tier=2 should pass {path}"
        assert resp.json()["tier"] == 2


# ---------------------------------------------------------------------------
# Token failure modes (401, NOT 402)
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_unauthenticated_request_returns_401(http_client) -> None:
    """No Authorization header → 401, never 402."""
    resp = await http_client.get("/api/_test/tier0")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "missing_token"


@pytest.mark.db
async def test_malformed_authorization_header_returns_401(http_client) -> None:
    http_client.headers["Authorization"] = "NotBearer something"
    try:
        resp = await http_client.get("/api/_test/tier0")
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "invalid_authorization_header"
    finally:
        http_client.headers.pop("Authorization", None)


@pytest.mark.db
async def test_garbage_token_returns_401(http_client) -> None:
    http_client.headers["Authorization"] = "Bearer not.a.real.jwt"
    try:
        resp = await http_client.get("/api/_test/tier0")
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "invalid_token"
    finally:
        http_client.headers.pop("Authorization", None)


@pytest.mark.db
async def test_refresh_token_used_as_access_returns_401(http_client) -> None:
    """Cross-kind reuse must fail at the boundary (kind discriminator)."""
    from app.core.security import create_refresh_token

    refresh = create_refresh_token(user_id="u-xyz")
    http_client.headers["Authorization"] = f"Bearer {refresh}"
    try:
        resp = await http_client.get("/api/_test/tier0")
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "invalid_token"
    finally:
        http_client.headers.pop("Authorization", None)
