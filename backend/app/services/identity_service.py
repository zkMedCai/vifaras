"""Identity service — Tier 1 upgrade via Self Protocol (brief task 2.3).

Async-first per brief §7. Uses AsyncSession + select() throughout. The
HTTP call to the Self verifier is an async httpx call inside a small
seam (`_post_to_self_verifier`) that test code can monkey-patch without
having to mock anything else.

Public surface:

- `SelfProofPayload`            — request shape from the mobile app
- `VerifiedIdentity`            — parsed verifier response (server-validated)
- `verify_self_proof(...)`      — calls the verifier, applies server-side
                                  invariants (isAdult, country, expiry,
                                  scope), raises typed errors on any
                                  failure mode.
- `upgrade_user_to_tier_1(...)` — atomic 0→1 transition: SELECT FOR UPDATE,
                                  idempotency, nullifier-collision check,
                                  KMS keypair generation, agent creation
                                  with `status='pending_mandate'`. All in
                                  one commit; audit emitted post-commit.

Error → HTTP mapping is owned by the API layer (`api/identity.py`):
  SelfVerifierUnavailable        → 500 (transient verifier outage)
  SelfVerificationFailed         → 422 (proof rejected by Self or by us)
  NullifierCollision             → 409 (different account, same document)
  InvalidTierTransition          → 409 (e.g. tier=2 trying to "upgrade" to 1)
  UserNotFound                   → 404
  KMSError                       → 500 (key generation failed; user untouched)

The atomic transition is documented in DESIGN_QUESTIONS DQ-11.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import log
from app.models.schema import Agent, User
from app.services import audit_service
from app.services.kms import get_kms


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IdentityError(Exception):
    """Base — every subclass carries `code` (str) and `http_status` (int)."""

    code: str = "identity_error"
    http_status: int = 400


class SelfVerifierUnavailable(IdentityError):
    """Verifier HTTP call failed (timeout, network, 5xx). Retry-able."""

    code = "verifier_unavailable"
    http_status = 500


class SelfVerificationFailed(IdentityError):
    """Verifier rejected the proof, or our server-side invariants did."""

    http_status = 422

    def __init__(self, error_code: str, message: str | None = None) -> None:
        self.code = f"self.{error_code.lower()}" if error_code else "self.unknown"
        self.error_code = error_code
        super().__init__(message or f"Self verification failed: {error_code}")


class NullifierCollision(IdentityError):
    """Another user is already bound to this document's nullifier."""

    code = "nullifier_collision"
    http_status = 409

    def __init__(self, *, requesting_user_id: str, existing_user_id: str) -> None:
        self.requesting_user_id = requesting_user_id
        self.existing_user_id = existing_user_id
        super().__init__("document already bound to another account")


class InvalidTierTransition(IdentityError):
    """Caller asked for 0→1 but the user is already at a higher tier (not 1)."""

    code = "invalid_tier_transition"
    http_status = 409


class UserNotFound(IdentityError):
    code = "user_not_found"
    http_status = 404


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class SelfProofPayload(BaseModel):
    """Payload the mobile app sends us, verbatim from Self mobile SDK."""

    proof: str = Field(min_length=1)
    public_signals: list[Any] = Field(default_factory=list, alias="publicSignals")

    model_config = {"populate_by_name": True}


@dataclass(frozen=True)
class VerifiedIdentity:
    """Server-side parsed view of a successful Self verification.

    `attributes` is the dict we'll persist on `User.attributes_proven` —
    after server-side validation, so it's safe to write through.
    """

    nullifier_hash: str
    attributes: dict[str, Any]
    document_expiry: datetime
    scope: str
    user_identifier: str


@dataclass
class Tier1UpgradeResult:
    """Outcome of `upgrade_user_to_tier_1`."""

    already_upgraded: bool
    user_id: str
    tier: int
    agent_id: str | None
    agent_pubkey: str | None
    nullifier_hash: str
    attributes_proven: dict[str, Any]


# ---------------------------------------------------------------------------
# Self verifier — HTTP seam (mockable)
# ---------------------------------------------------------------------------


async def _post_to_self_verifier(payload: dict[str, Any]) -> dict[str, Any]:
    """Low-level HTTP POST to the Self verifier. Mockable in tests.

    Tests should monkey-patch this exact function:
        monkeypatch.setattr(
            "app.services.identity_service._post_to_self_verifier",
            fake_post,
        )

    No retry here — `verify_self_proof` decides retry policy (V0: no retry,
    a proof that's invalid won't become valid by retrying; a transient
    network error lets the user retry from the app).
    """
    async with httpx.AsyncClient(
        timeout=settings.self_verifier_timeout_seconds
    ) as client:
        response = await client.post(
            settings.self_verifier_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_self_expiry(raw: Any) -> datetime:
    """Self returns ISO-8601 dates (e.g. "2030-04-15"). Parse to UTC datetime.

    Accepts both bare YYYY-MM-DD and full timestamps. Falls back to a
    SelfVerificationFailed if the verifier returns garbage."""
    if not isinstance(raw, str) or not raw:
        raise SelfVerificationFailed("DOCUMENT_EXPIRY_INVALID")
    try:
        if len(raw) == 10:  # YYYY-MM-DD
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SelfVerificationFailed("DOCUMENT_EXPIRY_INVALID", str(exc)) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _build_disclosure_requirements() -> dict[str, Any]:
    """V0 disclosure requirements — hard-coded per brief §3.

    minimumAge=18, issuingState=["IT"] (V0 geo-scope), documentValidity=true.
    """
    return {
        "minimumAge": 18,
        "issuingState": ["IT"],
        "documentValidity": True,
    }


async def verify_self_proof(
    *,
    proof: str,
    public_signals: list[Any],
    user_identifier: str,
) -> VerifiedIdentity:
    """Submit a proof to Self, validate server-side, return VerifiedIdentity.

    Server-side invariants enforced AFTER the verifier says `verified=True`:
      - `attributes.isAdult is True` (else SelfVerificationFailed.ISADULT_REQUIRED)
      - `attributes.issuingState == "IT"` (V0 geo-scope; SelfVerificationFailed.SCOPE_MISMATCH)
      - `attributes.documentValid is True` (SelfVerificationFailed.DOCUMENT_INVALID)
      - `attributes.documentExpiry > now` (SelfVerificationFailed.DOCUMENT_EXPIRED)
      - `scope` echoed by verifier == our configured scope (SelfVerificationFailed.SCOPE_MISMATCH)
      - `userIdentifier` echoed == what we sent (SelfVerificationFailed.USER_IDENTIFIER_MISMATCH)

    These are **belt-and-suspenders**: the verifier should reject a proof
    that fails any of these, but trusting Self alone for security-critical
    invariants is bad form. We re-check.
    """
    payload = {
        "proof": proof,
        "publicSignals": public_signals,
        "scope": settings.self_verifier_scope,
        "userIdentifier": user_identifier,
        "disclosureRequirements": _build_disclosure_requirements(),
    }

    try:
        raw = await _post_to_self_verifier(payload)
    except httpx.TimeoutException as exc:
        log.warning("self.timeout", url=settings.self_verifier_url, error=str(exc))
        raise SelfVerifierUnavailable("verifier timed out") from exc
    except httpx.HTTPStatusError as exc:
        log.warning(
            "self.http_error",
            status=exc.response.status_code,
            url=settings.self_verifier_url,
        )
        raise SelfVerifierUnavailable(
            f"verifier returned HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        log.warning("self.network_error", error=type(exc).__name__, message=str(exc))
        raise SelfVerifierUnavailable(str(exc)) from exc

    if not raw.get("verified"):
        error_code = raw.get("errorCode") or "PROOF_INVALID"
        log.info("self.verification_rejected", error_code=error_code)
        raise SelfVerificationFailed(error_code, raw.get("errorMessage"))

    attributes = raw.get("attributes") or {}
    nullifier = raw.get("nullifier")
    if not nullifier or not isinstance(nullifier, str):
        raise SelfVerificationFailed("NULLIFIER_MISSING")

    # Server-side scope echo check
    if raw.get("scope") != settings.self_verifier_scope:
        raise SelfVerificationFailed("SCOPE_MISMATCH")

    # Server-side userIdentifier echo check
    if raw.get("userIdentifier") != user_identifier:
        raise SelfVerificationFailed("USER_IDENTIFIER_MISMATCH")

    # Disclosure / attribute checks
    if attributes.get("isAdult") is not True:
        raise SelfVerificationFailed("ISADULT_REQUIRED")
    if attributes.get("issuingState") != "IT":
        raise SelfVerificationFailed("SCOPE_MISMATCH")
    if attributes.get("documentValid") is not True:
        raise SelfVerificationFailed("DOCUMENT_INVALID")

    expiry = _parse_self_expiry(attributes.get("documentExpiry"))
    if expiry <= _utcnow():
        raise SelfVerificationFailed("DOCUMENT_EXPIRED")

    # Reshape `attributes` into the persistence-friendly form documented
    # in brief §3 ("only flags, never personal data"). We keep the same
    # keys the verifier uses (camelCase) so the JSONB blob mirrors the
    # source of truth — easier to debug than translating field names.
    persisted_attributes = {
        "isAdult": True,
        "issuingState": attributes.get("issuingState"),
        "documentValid": True,
        "documentExpiry": attributes.get("documentExpiry"),
    }

    return VerifiedIdentity(
        nullifier_hash=nullifier,
        attributes=persisted_attributes,
        document_expiry=expiry,
        scope=raw.get("scope"),
        user_identifier=user_identifier,
    )


# ---------------------------------------------------------------------------
# Tier 0 → 1 atomic upgrade
# ---------------------------------------------------------------------------


async def upgrade_user_to_tier_1(
    db: AsyncSession,
    *,
    user_id: str,
    proof: SelfProofPayload,
) -> Tier1UpgradeResult:
    """Atomic transition tier 0 → 1 + agent keypair creation.

    Sequence:
      1. Verify Self proof (no DB activity, no row locks held)
      2. SELECT user FOR UPDATE (row lock held until commit/rollback)
      3. Idempotency: if tier ≥ 1, return already_upgraded=True with
         existing agent (rollback to release the lock cleanly)
      4. Tier guard: tier must be exactly 0 (defense vs corrupted state)
      5. Nullifier collision check (different user already bound to
         this document → 409)
      6. Generate ed25519 keypair via KMS (async, can fail; if it does,
         no user fields have been mutated — rollback releases the lock)
      7. Mutate user (tier, nullifier, attributes, timestamps)
      8. Create Agent row with status='pending_mandate'
      9. Commit
      10. Audit (post-commit, fire-and-forget)
    """
    # 1. Verify Self proof outside any DB transaction.
    verified = await verify_self_proof(
        proof=proof.proof,
        public_signals=proof.public_signals,
        user_identifier=user_id,
    )

    # 2. Lock the user row. SELECT FOR UPDATE prevents a concurrent upgrade
    #    request from also passing the tier-0 check and creating a duplicate
    #    agent (a real risk if the mobile app double-fires the request).
    user = await db.scalar(
        select(User).where(User.id == user_id).with_for_update()
    )
    if user is None:
        raise UserNotFound(user_id)

    # 3. Idempotency: already upgraded → no-op success.
    if user.tier >= 1:
        existing_agent = await db.scalar(
            select(Agent)
            .where(Agent.user_id == user.id)
            .order_by(Agent.created_at.asc())
        )
        return Tier1UpgradeResult(
            already_upgraded=True,
            user_id=user.id,
            tier=user.tier,
            agent_id=existing_agent.id if existing_agent else None,
            agent_pubkey=existing_agent.pubkey if existing_agent else None,
            nullifier_hash=user.nullifier_hash or verified.nullifier_hash,
            attributes_proven=dict(user.attributes_proven or {}),
        )

    # 4. Defensive tier guard (monotonic 0/1/2 — anything but 0 here is bad state).
    if user.tier != 0:
        raise InvalidTierTransition(
            f"cannot upgrade from tier {user.tier} to 1"
        )

    # 5. Nullifier collision: another user already bound to this document.
    collision = await db.scalar(
        select(User).where(User.nullifier_hash == verified.nullifier_hash)
    )
    if collision is not None:
        raise NullifierCollision(
            requesting_user_id=user.id,
            existing_user_id=collision.id,
        )

    # 6. Generate the agent keypair via KMS. Done BEFORE mutating user fields
    #    so a KMS failure leaves the open transaction with no pending writes
    #    (rollback is a no-op). The KMS row is staged on the same `db` session,
    #    so it commits atomically with the Agent insert below.
    kms_ref, pubkey_b64 = await get_kms().generate_agent_keypair(db)

    # 7. Mutate user.
    user.nullifier_hash = verified.nullifier_hash
    user.attributes_proven = verified.attributes
    user.attributes_verified_at = _utcnow().replace(tzinfo=None)  # schema is naive
    user.attributes_expires_at = verified.document_expiry.replace(tzinfo=None)
    user.tier = 1

    # 8. Create the dormant agent.
    agent = Agent(
        user_id=user.id,
        pubkey=pubkey_b64,
        privkey_kms_ref=kms_ref,
        status="pending_mandate",
    )
    db.add(agent)
    await db.flush()
    agent_id = agent.id

    # 9. Commit.
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # 10. Audit (post-commit, never raises — see audit_service docstring).
    await audit_service.log_tier_upgrade(
        user_id=user.id,
        from_tier=0,
        to_tier=1,
        nullifier_hash=verified.nullifier_hash,
        agent_id=agent_id,
    )

    return Tier1UpgradeResult(
        already_upgraded=False,
        user_id=user.id,
        tier=1,
        agent_id=agent_id,
        agent_pubkey=pubkey_b64,
        nullifier_hash=verified.nullifier_hash,
        attributes_proven=verified.attributes,
    )
