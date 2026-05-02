"""V0 LocalDBProvider: ed25519 keypairs encrypted at rest in `kms_agent_keys`.

The provider is the sole owner of the `kms_agent_keys` table — no other
service reads it. Callers receive opaque "db:<id>" refs from
`generate_agent_keypair` and round-trip them to `sign()`.

Trade-off: the master key shares a fate with the application process; a host
compromise reveals the key and therefore every privkey at rest. V0 accepts
this for a single-host deployment; V0.5+ swaps this provider for AWS KMS so
the master key never enters process memory.
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schema import KMSAgentKey
from app.services.kms.encryption import decrypt, encrypt
from app.services.kms.interface import KMSError, KMSProvider


_REF_PREFIX = "db:"


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _parse_ref(kms_ref: str) -> int:
    if not kms_ref.startswith(_REF_PREFIX):
        raise KMSError(f"unsupported kms_ref scheme: {kms_ref!r}")
    try:
        return int(kms_ref[len(_REF_PREFIX):])
    except ValueError as exc:
        raise KMSError(f"invalid kms_ref id: {kms_ref!r}") from exc


class LocalDBProvider(KMSProvider):
    async def generate_agent_keypair(self, db: AsyncSession) -> tuple[str, str]:
        privkey = Ed25519PrivateKey.generate()
        privkey_raw = privkey.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pubkey_raw = privkey.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        try:
            ciphertext, nonce = encrypt(privkey_raw)
        except KMSError:
            raise
        except Exception as exc:
            raise KMSError(f"keygen encrypt failed: {exc}") from exc

        row = KMSAgentKey(privkey_encrypted=ciphertext, nonce=nonce)
        db.add(row)
        # flush to materialise the autoincrement id without committing the
        # caller's transaction — commit boundary stays with the caller.
        await db.flush()
        return f"{_REF_PREFIX}{row.id}", _b64url_nopad(pubkey_raw)

    async def sign(self, db: AsyncSession, kms_ref: str, message: bytes) -> bytes:
        key_id = _parse_ref(kms_ref)
        row = await db.get(KMSAgentKey, key_id)
        if row is None:
            raise KMSError(f"kms key not found: id={key_id}")
        privkey_raw = decrypt(bytes(row.privkey_encrypted), bytes(row.nonce))
        privkey = Ed25519PrivateKey.from_private_bytes(privkey_raw)
        return privkey.sign(message)
