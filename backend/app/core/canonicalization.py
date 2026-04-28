"""JSON canonicalization for signatures (RFC 8785 / JCS).

Why we need it: a WebAuthn signature is over a hash of `clientDataJSON`,
which contains the challenge. We use the challenge to pin a SPECIFIC byte
sequence as "the signed mandate" — but only if both /draft and /submit
serialize the same payload to the same bytes. Standard `json.dumps()`
doesn't guarantee this (key order, whitespace, number formatting all vary).

RFC 8785 (JSON Canonicalization Scheme) defines a deterministic mapping:
- UTF-8 encoding
- Sorted object keys (lexicographic, codepoint order)
- No insignificant whitespace
- Specific number serialization rules (ECMAScript ToString)

We use the `jcs` library (~50 LOC, no transitive deps).

`canonicalize(payload)` returns the canonical bytes — store these on the
`MandateDraft.canonical_payload` column verbatim, never re-canonicalize.
At submit time the signature is validated against these EXACT bytes.

`digest(canonical_bytes)` returns the SHA-256 of the canonical bytes —
this is what `webauthn` accepts as `expected_challenge` (it's what the
authenticator effectively signs over, via clientDataJSON containing the
b64url of this digest).
"""
from __future__ import annotations

import hashlib
from typing import Any

import jcs


def canonicalize(payload: dict[str, Any]) -> bytes:
    """Return the RFC 8785 canonical UTF-8 bytes of `payload`.

    The bytes are stable: same input dict ⇒ same output bytes, byte-for-byte.
    Store the result; do not re-canonicalize for verification (in case the
    library's behavior shifts in a minor version).
    """
    return jcs.canonicalize(payload)


def digest(canonical_bytes: bytes) -> bytes:
    """Return SHA-256 of canonical bytes (32 bytes)."""
    return hashlib.sha256(canonical_bytes).digest()
