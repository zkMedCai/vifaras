"""Rate limiting deep coverage (brief task 7.1.2).

Three concerns:

  1. **Auth endpoints** (`/api/auth/*`): IP-keyed since callers are
     unauthenticated at this stage. One parametrized test per endpoint
     verifies the second call after a `1/minute` override returns 429
     with the canonical envelope.

  2. **Authenticated CRUD** (intents/match/deals/negotiations): per-user
     keying via `user_key`. Each endpoint verifies 429 on burst when the
     limit is squeezed to `1/minute`. The 4xx body of the first call is
     irrelevant — what matters is the limiter counted the attempt.

  3. **Per-user isolation**: two distinct users on the same transport
     get independent buckets; user A burning their cap doesn't block
     user B. Plus three unit tests on `user_key` directly to cover the
     `Authorization` header parsing branches that the HTTP integration
     tests can't exercise (auth dep short-circuits 401 before the
     limiter wrapper runs when JWT is absent/malformed).
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from starlette.requests import Request as StarletteRequest

from app.core.config import settings
from app.core.rate_limit import limiter, user_key
from app.core.security import create_access_token


# ---------------------------------------------------------------------------
# Auth endpoints (IP-keyed)
# ---------------------------------------------------------------------------


_FAKE_ASSERTION = {
    "id": "x",
    "rawId": "x",
    "type": "public-key",
    "response": {},
}


# (id, method, path, body, setting_name)
_AUTH_ENDPOINTS: list[tuple[str, str, str, dict[str, Any], str]] = [
    (
        "register_begin",
        "post",
        "/api/auth/register/begin",
        {"email": "rl-test@example.com"},
        "rate_limit_auth_strict",
    ),
    (
        "register_complete",
        "post",
        "/api/auth/register/complete",
        {"credential": {}, "challenge_token": "tok"},
        "rate_limit_auth_strict",
    ),
    (
        "login_begin",
        "post",
        "/api/auth/login/begin",
        {"email": "rl-test@example.com"},
        "rate_limit_auth_normal",
    ),
    (
        "login_complete",
        "post",
        "/api/auth/login/complete",
        {"credential": {}, "challenge_token": "tok"},
        "rate_limit_auth_normal",
    ),
    (
        "refresh",
        "post",
        "/api/auth/refresh",
        {"refresh_token": "tok"},
        "rate_limit_auth_refresh",
    ),
]


@pytest.mark.db
@pytest.mark.parametrize(
    "method,path,body,setting",
    [(m, p, b, s) for _, m, p, b, s in _AUTH_ENDPOINTS],
    ids=[name for name, *_ in _AUTH_ENDPOINTS],
)
async def test_auth_endpoint_rate_limited_per_ip(
    enable_limiter, monkeypatch, http_client, method, path, body, setting
):
    monkeypatch.setattr(settings, setting, "1/minute")
    limiter.reset()  # pick up the squeezed limit

    fn = getattr(http_client, method)
    r1 = await fn(path, json=body)
    r2 = await fn(path, json=body)

    # First call may 4xx (bad token / missing user) — the only assertion
    # that matters is "the limiter counted it, then 429'd the second."
    assert r1.status_code != 429, r1.text
    assert r2.status_code == 429, r2.text
    assert r2.json()["code"] == "rate_limited"


# ---------------------------------------------------------------------------
# Authenticated endpoints (user-keyed)
# ---------------------------------------------------------------------------


# (id, method, path, body, tier, setting_name)
_USER_ENDPOINTS: list[
    tuple[str, str, str, dict[str, Any] | None, int, str]
] = [
    # intents — POST already covered in test_pre_frontend
    (
        "intents_list",
        "get",
        "/api/intents",
        None,
        0,
        "rate_limit_user_read",
    ),
    (
        "intents_get",
        "get",
        f"/api/intents/{uuid.uuid4()}",
        None,
        0,
        "rate_limit_user_read",
    ),
    (
        "intents_patch",
        "patch",
        f"/api/intents/{uuid.uuid4()}",
        {"title": "rl-test"},
        0,
        "rate_limit_post_strict",
    ),
    (
        "intents_delete",
        "delete",
        f"/api/intents/{uuid.uuid4()}",
        None,
        0,
        "rate_limit_post_strict",
    ),
    # matches
    (
        "matches_list",
        "get",
        f"/api/intents/{uuid.uuid4()}/matches",
        None,
        0,
        "rate_limit_user_read",
    ),
    (
        "matches_detail",
        "get",
        f"/api/matches/{uuid.uuid4()}",
        None,
        2,
        "rate_limit_user_read",
    ),
    # deals
    (
        "deals_list",
        "get",
        "/api/deals",
        None,
        2,
        "rate_limit_user_read",
    ),
    (
        "deals_get",
        "get",
        f"/api/deals/{uuid.uuid4()}",
        None,
        2,
        "rate_limit_user_read",
    ),
    (
        "deals_sign_draft",
        "post",
        f"/api/deals/{uuid.uuid4()}/sign/draft",
        {},
        2,
        "rate_limit_post_strict",
    ),
    (
        "deals_sign_submit",
        "post",
        f"/api/deals/{uuid.uuid4()}/sign/submit",
        {
            "draft_id": str(uuid.uuid4()),
            "webauthn_assertion": _FAKE_ASSERTION,
        },
        2,
        "rate_limit_mandate_critical",
    ),
    (
        "deals_cancel_draft",
        "post",
        f"/api/deals/{uuid.uuid4()}/cancel/draft",
        {},
        2,
        "rate_limit_post_strict",
    ),
    (
        "deals_cancel_submit",
        "post",
        f"/api/deals/{uuid.uuid4()}/cancel/submit",
        {
            "draft_id": str(uuid.uuid4()),
            "webauthn_assertion": _FAKE_ASSERTION,
        },
        2,
        "rate_limit_mandate_critical",
    ),
    (
        "deals_msg_send",
        "post",
        f"/api/deals/{uuid.uuid4()}/messages",
        {"encrypted_content_b64": "AA==", "nonce_b64": "AA=="},
        2,
        "rate_limit_post_strict",
    ),
    (
        "deals_msg_list",
        "get",
        f"/api/deals/{uuid.uuid4()}/messages",
        None,
        2,
        "rate_limit_user_read",
    ),
    # negotiations
    (
        "nego_start",
        "post",
        "/api/negotiations",
        {
            "match_id": str(uuid.uuid4()),
            "agent_id": str(uuid.uuid4()),
            "price_cents": 100,
        },
        1,
        "rate_limit_post_strict",
    ),
    (
        "nego_accept",
        "post",
        f"/api/negotiations/{uuid.uuid4()}/accept",
        {"agent_id": str(uuid.uuid4())},
        2,
        "rate_limit_post_strict",
    ),
    (
        "nego_reject",
        "post",
        f"/api/negotiations/{uuid.uuid4()}/reject",
        {"agent_id": str(uuid.uuid4())},
        1,
        "rate_limit_post_strict",
    ),
    (
        "nego_get",
        "get",
        f"/api/negotiations/{uuid.uuid4()}",
        None,
        1,
        "rate_limit_user_read",
    ),
    (
        "nego_list",
        "get",
        "/api/negotiations",
        None,
        1,
        "rate_limit_user_read",
    ),
]


@pytest.mark.db
@pytest.mark.parametrize(
    "method,path,body,tier,setting",
    [(m, p, b, t, s) for _, m, p, b, t, s in _USER_ENDPOINTS],
    ids=[name for name, *_ in _USER_ENDPOINTS],
)
async def test_authenticated_endpoint_rate_limited_per_user(
    enable_limiter,
    monkeypatch,
    authenticated_client,
    method,
    path,
    body,
    tier,
    setting,
):
    monkeypatch.setattr(settings, setting, "1/minute")
    limiter.reset()

    client, _ = authenticated_client(tier=tier)
    fn = getattr(client, method)
    if body is not None:
        r1 = await fn(path, json=body)
        r2 = await fn(path, json=body)
    else:
        r1 = await fn(path)
        r2 = await fn(path)

    assert r1.status_code != 429, (
        f"First call should not be 429, got {r1.status_code}: {r1.text}"
    )
    assert r2.status_code == 429, (
        f"Second call should be 429, got {r2.status_code}: {r2.text}"
    )
    assert r2.json()["code"] == "rate_limited"


# ---------------------------------------------------------------------------
# Per-user keying — isolation
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_user_key_isolates_two_users_on_same_ip(
    enable_limiter, monkeypatch, http_client
):
    """Two distinct users sharing the transport (same IP) get independent
    buckets — so user A burning the cap doesn't block user B."""
    monkeypatch.setattr(settings, "rate_limit_user_read", "1/minute")
    limiter.reset()

    user_a = str(uuid.uuid4())
    user_b = str(uuid.uuid4())
    token_a = create_access_token(user_id=user_a, tier=0)
    token_b = create_access_token(user_id=user_b, tier=0)

    # User A: first call passes (empty list), second is 429.
    http_client.headers["Authorization"] = f"Bearer {token_a}"
    r_a1 = await http_client.get("/api/intents")
    r_a2 = await http_client.get("/api/intents")
    assert r_a1.status_code != 429
    assert r_a2.status_code == 429

    # User B: independent bucket, first call still passes.
    http_client.headers["Authorization"] = f"Bearer {token_b}"
    r_b = await http_client.get("/api/intents")
    assert r_b.status_code != 429, r_b.text


# ---------------------------------------------------------------------------
# user_key unit tests — cover branches the HTTP path can't reach
# ---------------------------------------------------------------------------
#
# When the auth dependency on a route raises 401 (missing/invalid JWT),
# FastAPI short-circuits before the slowapi wrapper runs — so the
# `user_key` fallback-to-IP path can't be observed via HTTP. Unit tests
# on the function directly cover that branch.


def _request_with_headers(headers: list[tuple[bytes, bytes]]) -> StarletteRequest:
    scope = {
        "type": "http",
        "headers": headers,
        "client": ("203.0.113.42", 12345),
    }
    return StarletteRequest(scope)


def test_user_key_extracts_user_id_from_valid_bearer():
    user_id = str(uuid.uuid4())
    token = create_access_token(user_id=user_id, tier=2)
    req = _request_with_headers(
        [(b"authorization", f"Bearer {token}".encode())]
    )
    assert user_key(req) == f"user:{user_id}"


def test_user_key_falls_back_to_ip_when_no_authorization_header():
    req = _request_with_headers([])
    # Falls back to remote address — should be the client IP, not "user:..."
    assert user_key(req) == "203.0.113.42"


def test_user_key_falls_back_to_ip_when_token_malformed():
    req = _request_with_headers(
        [(b"authorization", b"Bearer not-a-jwt-at-all")]
    )
    assert user_key(req) == "203.0.113.42"


def test_user_key_falls_back_to_ip_when_scheme_not_bearer():
    req = _request_with_headers([(b"authorization", b"Basic dXNlcjpwdw==")])
    assert user_key(req) == "203.0.113.42"


def test_user_key_falls_back_when_token_uses_wrong_kind():
    """A JWT with kind != 'access' must fall back — the `_decode` strict-kind check rejects it.

    Pre-[7.4.2] this used `create_refresh_token` (refresh was a JWT then);
    post-[7.4.2] refresh is opaque DB-backed, so we hand-craft a JWT with a
    non-access kind to exercise the same fallback branch.
    """
    import jwt as pyjwt

    from app.core.config import settings

    bad_kind_jwt = pyjwt.encode(
        {"sub": str(uuid.uuid4()), "kind": "refresh"},
        settings.jwt_secret_current,
        algorithm=settings.jwt_alg,
    )
    req = _request_with_headers(
        [(b"authorization", f"Bearer {bad_kind_jwt}".encode())]
    )
    assert user_key(req) == "203.0.113.42"
