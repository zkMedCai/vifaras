"""KMS package — per-agent ed25519 keypair custody behind a pluggable provider.

Public surface:
- `get_kms()` — process-wide provider instance (V0: LocalDBProvider).
- `KMSProvider` — abstract interface; depend on this in service layers.
- `KMSError` — raised by all provider operations on failure.
- `load_pubkey_b64(s)` — parse a base64url-encoded ed25519 pubkey.
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.services.kms.interface import KMSError, KMSProvider
from app.services.kms.local_db_provider import LocalDBProvider


_provider: KMSProvider | None = None


def get_kms() -> KMSProvider:
    """Return the process-wide KMS provider instance (lazily constructed)."""
    global _provider
    if _provider is None:
        _provider = LocalDBProvider()
    return _provider


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + padding).encode("ascii"))


def load_pubkey_b64(pubkey_b64: str) -> Ed25519PublicKey:
    """Reconstruct an Ed25519 public key from the base64url form stored on Agent.pubkey.

    Pure utility — no provider state, no IO. Identical across providers, so
    it lives at package level rather than on KMSProvider.
    """
    return Ed25519PublicKey.from_public_bytes(_b64url_decode(pubkey_b64))


__all__ = ["KMSError", "KMSProvider", "get_kms", "load_pubkey_b64"]
