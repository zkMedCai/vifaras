"""AES-256-GCM envelope encryption for KMS key material ([7.4.1]).

Master key sourced from settings (`KMS_MASTER_KEY` env var, base64-encoded
32 bytes). Per-call random 12-byte nonce (NIST SP 800-38D recommendation —
nonce reuse with the same key would catastrophically break confidentiality
and authenticity, so each encrypt draws fresh randomness).

V0: master key in env, validated at lifespan startup.
V0.5+: cloud KMS Encrypt/Decrypt (AWS KMS) — the master key never enters the
application process.
"""
from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings
from app.services.kms.interface import KMSError


_NONCE_SIZE = 12  # AES-GCM standard nonce size
_MASTER_KEY_SIZE = 32  # AES-256


def load_master_key() -> bytes:
    """Decode the master key from settings and validate length.

    Called on every encrypt/decrypt rather than cached: base64 decode is
    microseconds, and skipping the cache simplifies test fixtures that swap
    the master key per session.

    V0.5+ caveat: if `sign()` becomes a hot path (e.g. A2A messaging at scale),
    revisit and add an in-memory cache with a test-fixture invalidation hook.
    """
    raw_b64 = settings.kms_master_key
    if not raw_b64:
        raise KMSError(
            "KMS_MASTER_KEY env var not set. "
            "Bootstrap with: openssl rand -base64 32"
        )
    try:
        decoded = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KMSError(f"KMS_MASTER_KEY is not valid base64: {exc}") from exc
    if len(decoded) != _MASTER_KEY_SIZE:
        raise KMSError(
            f"KMS_MASTER_KEY must decode to {_MASTER_KEY_SIZE} bytes, got {len(decoded)}"
        )
    return decoded


def validate_master_key() -> None:
    """Fail-fast probe used at lifespan startup. Raises KMSError on misconfiguration."""
    load_master_key()


def encrypt(plaintext: bytes) -> tuple[bytes, bytes]:
    """Encrypt `plaintext` with the master key. Returns (ciphertext, nonce).

    Authentication tag is appended to the ciphertext by AESGCM — store both
    columns side-by-side; lose either and decrypt will fail.
    """
    master_key = load_master_key()
    nonce = os.urandom(_NONCE_SIZE)
    ciphertext = AESGCM(master_key).encrypt(nonce, plaintext, associated_data=None)
    return ciphertext, nonce


def decrypt(ciphertext: bytes, nonce: bytes) -> bytes:
    """Decrypt `(ciphertext, nonce)` with the master key. Returns plaintext.

    Raises KMSError on auth tag mismatch (wrong master key, swapped or tampered
    ciphertext, corrupted nonce). Error message stays generic to avoid leaking
    which failure mode tripped.
    """
    master_key = load_master_key()
    try:
        return AESGCM(master_key).decrypt(nonce, ciphertext, associated_data=None)
    except InvalidTag as exc:
        raise KMSError("decrypt failed: authentication tag mismatch") from exc
