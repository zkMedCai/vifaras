"""JWT secret rotation overlap window tests ([7.4.3]).

Coverage:
  1. _encode signs with current_secret only
  2. _decode succeeds against current_secret (steady state)
  3. _decode falls back to previous_secret during rotation window
  4. _decode does NOT fall back when previous_secret is empty (steady state)
  5. _decode increments JWT_DECODE_FALLBACK_TOTAL on fallback success
  6. _decode does NOT increment the counter on current-secret success
  7. _decode raises InvalidTokenError when both secrets fail
  8. _decode short-circuits on ExpiredSignatureError (no wasted fallback)
  9. _decode raises kind mismatch separately from signature errors
 10. Lock contract α: challenge tokens use the same rotation pool as access
"""
from __future__ import annotations

from datetime import datetime, timedelta

import jwt
import pytest

from app.core.config import settings
from app.core.metrics import JWT_DECODE_FALLBACK_TOTAL
from app.core.security import _decode, _encode


# pyjwt warns on HMAC keys shorter than 32 bytes for SHA256 (RFC 7518 §3.2);
# the test secrets are sized accordingly so the warnings stay out of the suite.
_CURRENT_SECRET = "test-current-secret-padded-32bytes"
_PREVIOUS_SECRET = "test-previous-secret-padded-32bytes"
_NEW_CURRENT_SECRET = "test-new-current-secret-padded-32bytes"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secrets_baseline(monkeypatch):
    """Steady state: only `current` is set, `previous` is empty."""
    monkeypatch.setattr(settings, "jwt_secret_current", _CURRENT_SECRET)
    monkeypatch.setattr(settings, "jwt_secret_previous", "")


@pytest.fixture
def secrets_during_rotation(monkeypatch):
    """Mid-rotation: `current` is the new secret, `previous` is the old one."""
    monkeypatch.setattr(settings, "jwt_secret_current", _NEW_CURRENT_SECRET)
    monkeypatch.setattr(settings, "jwt_secret_previous", _PREVIOUS_SECRET)


def _payload(*, kind: str = "access", ttl_min: int = 15) -> dict:
    return {
        "sub": "user-uuid",
        "kind": kind,
        "exp": datetime.utcnow() + timedelta(minutes=ttl_min),
    }


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------


def test_encode_uses_current_secret(secrets_baseline):
    token = _encode(_payload())

    # External verify with current → succeeds.
    payload = jwt.decode(token, _CURRENT_SECRET, algorithms=["HS256"])
    assert payload["sub"] == "user-uuid"

    # External verify with anything else → InvalidSignatureError.
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "wrong-secret-padded-out-to-32-bytes", algorithms=["HS256"])


# ---------------------------------------------------------------------------
# Decode — current path
# ---------------------------------------------------------------------------


def test_decode_with_current_secret_succeeds(secrets_baseline):
    token = _encode(_payload())
    payload = _decode(token, expected_kind="access")
    assert payload["sub"] == "user-uuid"


def test_decode_no_fallback_no_increment(secrets_baseline):
    """Decoding via the current path must NOT bump the fallback counter."""
    metric_before = JWT_DECODE_FALLBACK_TOTAL._value.get()

    token = _encode(_payload())
    _decode(token, expected_kind="access")

    metric_after = JWT_DECODE_FALLBACK_TOTAL._value.get()
    assert metric_after == metric_before


# ---------------------------------------------------------------------------
# Decode — fallback path
# ---------------------------------------------------------------------------


def test_decode_falls_back_to_previous_when_active(secrets_during_rotation):
    """A token signed with the old secret decodes via the previous fallback."""
    old_token = jwt.encode(
        _payload(),
        _PREVIOUS_SECRET,
        algorithm="HS256",
    )
    payload = _decode(old_token, expected_kind="access")
    assert payload["sub"] == "user-uuid"


def test_decode_fallback_increments_metric(secrets_during_rotation):
    metric_before = JWT_DECODE_FALLBACK_TOTAL._value.get()

    old_token = jwt.encode(
        _payload(),
        _PREVIOUS_SECRET,
        algorithm="HS256",
    )
    _decode(old_token, expected_kind="access")

    metric_after = JWT_DECODE_FALLBACK_TOTAL._value.get()
    assert metric_after == metric_before + 1


def test_decode_does_not_fall_back_when_previous_empty(secrets_baseline):
    """No `previous` set → a foreign-signed token must fail outright."""
    foreign_token = jwt.encode(
        _payload(),
        "foreign-secret-padded-out-to-32bytes",
        algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidTokenError):
        _decode(foreign_token, expected_kind="access")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_decode_invalid_token_raises_after_all_attempts(secrets_during_rotation):
    """Random secret → both current and previous fail → raise."""
    foreign_token = jwt.encode(
        _payload(),
        "unrelated-secret-padded-to-32-bytes!",
        algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidTokenError):
        _decode(foreign_token, expected_kind="access")


def test_decode_expired_token_short_circuits_no_fallback(secrets_during_rotation):
    """Signature OK on current but `exp` past → ExpiredSignatureError immediately.

    The fallback would not change the outcome (the same expired token would
    just fail signature on `previous`), so the loop must not waste an attempt.
    """
    expired_token = _encode(_payload(ttl_min=-1))
    with pytest.raises(jwt.ExpiredSignatureError):
        _decode(expired_token, expected_kind="access")


def test_decode_kind_mismatch_raises(secrets_baseline):
    """Signature OK but `kind` does not match expected → kind error, not signature."""
    token = _encode(_payload(kind="challenge", ttl_min=5))
    with pytest.raises(jwt.InvalidTokenError, match="kind="):
        _decode(token, expected_kind="access")


# ---------------------------------------------------------------------------
# Lock contract α — challenge tokens share the rotation pool
# ---------------------------------------------------------------------------


def test_decode_challenge_token_falls_back_to_previous(secrets_during_rotation):
    """Challenge tokens go through the same _decode gateway and inherit fallback."""
    old_challenge = jwt.encode(
        _payload(kind="challenge", ttl_min=5),
        _PREVIOUS_SECRET,
        algorithm="HS256",
    )
    payload = _decode(old_challenge, expected_kind="challenge")
    assert payload["sub"] == "user-uuid"
