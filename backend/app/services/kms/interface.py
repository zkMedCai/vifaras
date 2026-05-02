"""KMS provider interface — abstracts per-agent ed25519 keypair custody.

Implementation backends (V0: LocalDBProvider; V0.5+: AWSKMSProvider, VaultProvider)
plug in behind this contract. Service layers depend only on `KMSProvider`, never
on a concrete implementation.

The `kms_ref` returned by `generate_agent_keypair` is opaque to callers: today
"db:<id>", tomorrow potentially "aws:<arn>". It is stored on
`Agent.privkey_kms_ref` and round-tripped to `sign()` whenever the agent needs
to authorise something.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from sqlalchemy.ext.asyncio import AsyncSession


class KMSError(Exception):
    """Raised when a KMS operation fails (encrypt, decrypt, missing key, bad ref).

    Distinct from network/HTTP errors — surfaces as 500 to the API caller and
    triggers transaction rollback in any service that catches it (canonical
    case: identity_service tier-upgrade — KMS failure must leave the user
    untouched, no partial Agent row).
    """


class KMSProvider(ABC):
    """Abstract custody for per-agent ed25519 keypairs.

    Each call to `generate_agent_keypair` produces a fresh keypair; the privkey
    is persisted by the provider (encrypted at rest, never plaintext from V0+),
    the pubkey is returned for the Agent row, and an opaque `kms_ref` binds
    the two for later `sign()` calls.

    Both methods take the caller's `AsyncSession` so persistence/lookup commits
    atomically with the surrounding business write (e.g. the Agent row insert
    in identity_service).
    """

    @abstractmethod
    async def generate_agent_keypair(self, db: AsyncSession) -> tuple[str, str]:
        """Generate a fresh ed25519 keypair, persist privkey encrypted, return (kms_ref, pubkey_b64)."""

    @abstractmethod
    async def sign(self, db: AsyncSession, kms_ref: str, message: bytes) -> bytes:
        """Sign `message` with the privkey identified by `kms_ref`. Returns raw signature bytes.

        Raises KMSError if `kms_ref` is malformed, the row is missing, or the
        master key cannot decrypt the stored material.
        """
