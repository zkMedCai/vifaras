"""KMS stub — agent keypair custody (V0 file-based, brief task 2.3).

V0 implementation: each generated keypair is an ed25519 pair. The private key
is serialized as raw bytes (32B) and persisted in a per-key JSON file under
`settings.kms_keys_dir` (default `.secrets/agent_keys/`, gitignored). Real
KMS (AWS KMS / GCP KMS / HSM) is a V1 swap of this module.

Public surface:

- `generate_agent_keypair()` — generates a fresh ed25519 keypair, persists
  the private key locally, returns `(pubkey_b64, kms_ref)`. The `kms_ref`
  is opaque to callers; today it's `file:<path>`, tomorrow it could be
  `arn:aws:kms:...`. Callers store it on `Agent.privkey_kms_ref`.
- `sign(kms_ref, message)` — placeholder for the future signing seam used
  by negotiation/deal services. Not exercised in 2.3 but defined here so
  the interface is settled before 5.x.

The KMS does NOT know about agents or users. It owns key material and
returns opaque references — the DB binds the reference to an Agent row.
"""
from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.core.config import settings


_KMS_REF_FILE_PREFIX = "file:"


class KMSError(Exception):
    """Raised when the KMS stub fails to generate or read a key.

    Distinct from network/HTTP errors — surfaces as 500 to the caller and
    triggers a transaction rollback in the identity service (the user must
    not get tier=1 if the agent's keypair couldn't be persisted)."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _keys_dir() -> Path:
    """Resolve and ensure the configured keys directory exists."""
    path = Path(settings.kms_keys_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key_file_path(key_id: str) -> Path:
    if "/" in key_id or ".." in key_id:
        raise KMSError(f"refusing path traversal in key_id={key_id!r}")
    return _keys_dir() / f"{key_id}.json"


async def generate_agent_keypair() -> tuple[str, str]:
    """Generate a fresh ed25519 keypair, persist privkey, return (pubkey_b64, kms_ref).

    The keypair is ed25519. The private key is serialized as 32 raw bytes
    (Ed25519 native scalar) and stored in a JSON file. `kms_ref` is
    `file:<absolute-path>` — opaque to callers. The pubkey is returned
    base64url-no-padding for direct storage on `Agent.pubkey`.

    V0: file IO is sync inside an async function. For 100 users and
    sub-millisecond writes, the event-loop block is negligible. V1 swap
    to AWS/GCP KMS will move the call off-process anyway.
    """
    try:
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

        key_id = str(uuid.uuid4())
        path = _key_file_path(key_id)
        # Wrote-once-then-readonly: 0600 is set after open via os.chmod
        # would be the next refinement; for V0 dev the umask is enough.
        path.write_text(
            json.dumps(
                {
                    "alg": "ed25519",
                    "key_id": key_id,
                    "private_key_b64": _b64(privkey_raw),
                    "public_key_b64": _b64(pubkey_raw),
                }
            )
        )
        return _b64(pubkey_raw), f"{_KMS_REF_FILE_PREFIX}{path}"
    except KMSError:
        raise
    except Exception as exc:
        raise KMSError(f"keygen failed: {exc}") from exc


def _load_privkey_from_ref(kms_ref: str) -> Ed25519PrivateKey:
    if not kms_ref.startswith(_KMS_REF_FILE_PREFIX):
        raise KMSError(f"unsupported kms_ref scheme: {kms_ref!r}")
    path = Path(kms_ref[len(_KMS_REF_FILE_PREFIX) :])
    if not path.is_file():
        raise KMSError(f"key file missing: {path}")
    payload = json.loads(path.read_text())
    if payload.get("alg") != "ed25519":
        raise KMSError(f"unexpected alg: {payload.get('alg')!r}")
    raw = _b64_decode(payload["private_key_b64"])
    return Ed25519PrivateKey.from_private_bytes(raw)


async def sign(kms_ref: str, message: bytes) -> str:
    """Sign a message with the agent's private key. Returns base64url signature.

    Placeholder for 5.x (negotiation message signing). Lives here so the
    interface is settled before negotiation tooling needs it.
    """
    privkey = _load_privkey_from_ref(kms_ref)
    return _b64(privkey.sign(message))


def load_pubkey(pubkey_b64: str) -> Ed25519PublicKey:
    """Reconstruct an Ed25519 public key from the b64 stored on Agent.pubkey."""
    return Ed25519PublicKey.from_public_bytes(_b64_decode(pubkey_b64))
