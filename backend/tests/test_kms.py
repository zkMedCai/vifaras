"""KMS provider tests ([7.4.1]).

Coverage:
  1. generate_agent_keypair returns well-shaped (kms_ref, pubkey_b64) tuple
  2. privkey is encrypted at rest (row.privkey_encrypted ≠ raw 32 bytes)
  3. sign() output verifies with the returned pubkey (full roundtrip)
  4. sign() with unknown id raises KMSError
  5. sign() with malformed kms_ref raises KMSError (bad scheme / bad id)
  6. sign() raises if the master key changed between generate and sign
  7. validate_master_key rejects missing / wrong-size master key
"""
from __future__ import annotations

import base64
import secrets

import pytest

from app.services.kms import get_kms, load_pubkey_b64
from app.services.kms.encryption import validate_master_key
from app.services.kms.interface import KMSError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fresh_key_b64() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


@pytest.fixture
def fresh_master_key(monkeypatch) -> str:
    """Set a fresh 32-byte master key for the duration of the test.

    Both `monkeypatch.setenv` and the cached `settings.kms_master_key`
    attribute are updated; the latter restores automatically on teardown.
    """
    from app.core.config import settings

    key_b64 = _fresh_key_b64()
    monkeypatch.setenv("KMS_MASTER_KEY", key_b64)
    monkeypatch.setattr(settings, "kms_master_key", key_b64)
    return key_b64


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_generate_keypair_returns_db_ref_and_pubkey(
    fresh_master_key, async_db_session
):
    kms_ref, pubkey_b64 = await get_kms().generate_agent_keypair(async_db_session)

    assert kms_ref.startswith("db:")
    assert int(kms_ref.removeprefix("db:")) > 0

    # ed25519 pubkey is 32 raw bytes; encoded base64url-no-pad.
    pubkey_raw = base64.urlsafe_b64decode(pubkey_b64 + "==")
    assert len(pubkey_raw) == 32


@pytest.mark.db
async def test_generate_keypair_persists_encrypted_in_db(
    fresh_master_key, async_db_session
):
    from app.models.schema import KMSAgentKey

    kms_ref, _ = await get_kms().generate_agent_keypair(async_db_session)
    key_id = int(kms_ref.removeprefix("db:"))

    row = await async_db_session.get(KMSAgentKey, key_id)
    assert row is not None

    # AES-GCM ciphertext over a 32-byte plaintext is 32 + 16 (auth tag) = 48 bytes.
    # The point of this assertion is "≠ raw 32 bytes": encryption-at-rest holds.
    assert len(bytes(row.privkey_encrypted)) == 48
    assert len(bytes(row.nonce)) == 12


@pytest.mark.db
async def test_sign_and_verify_roundtrip(fresh_master_key, async_db_session):
    kms = get_kms()
    kms_ref, pubkey_b64 = await kms.generate_agent_keypair(async_db_session)

    message = b"vifaras kms roundtrip"
    signature = await kms.sign(async_db_session, kms_ref, message)

    # Raises cryptography.exceptions.InvalidSignature on mismatch.
    load_pubkey_b64(pubkey_b64).verify(signature, message)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_sign_with_unknown_id_raises(fresh_master_key, async_db_session):
    with pytest.raises(KMSError, match="not found"):
        await get_kms().sign(async_db_session, "db:99999999", b"m")


@pytest.mark.db
@pytest.mark.parametrize(
    "bad_ref",
    ["not-a-valid-ref", "file:/legacy/path.json", "db:not-int", "", "db:"],
)
async def test_sign_with_malformed_ref_raises(
    fresh_master_key, async_db_session, bad_ref
):
    with pytest.raises(KMSError):
        await get_kms().sign(async_db_session, bad_ref, b"m")


@pytest.mark.db
async def test_sign_with_wrong_master_key_raises(
    fresh_master_key, async_db_session, monkeypatch
):
    """If the master key changes between generate and sign, decrypt fails."""
    from app.core.config import settings

    kms = get_kms()
    kms_ref, _ = await kms.generate_agent_keypair(async_db_session)

    # Rotate the master key under the provider's feet.
    monkeypatch.setattr(settings, "kms_master_key", _fresh_key_b64())

    with pytest.raises(KMSError, match="authentication tag mismatch"):
        await kms.sign(async_db_session, kms_ref, b"m")


# ---------------------------------------------------------------------------
# Lifespan validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, error_match",
    [
        ("", "not set"),
        # 16 bytes decoded — wrong size for AES-256 (which wants 32).
        (base64.b64encode(b"x" * 16).decode("ascii"), "32 bytes"),
        ("not!valid!base64!", "not valid base64"),
    ],
)
def test_validate_master_key_rejects_invalid(monkeypatch, value, error_match):
    from app.core.config import settings

    monkeypatch.setattr(settings, "kms_master_key", value)
    with pytest.raises(KMSError, match=error_match):
        validate_master_key()
