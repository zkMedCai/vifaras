"""Refresh token lifecycle: issue, consume (rotate), invalidate ([7.4.2]).

Refresh tokens are opaque random strings (`secrets.token_urlsafe(32)`); only
the SHA-256 hex digest is stored in `refresh_tokens.token_hash`, so a DB
compromise does not yield usable tokens.

Each `consume_refresh_token` call rotates the chain: the presented token's
row flips to 'consumed', a new 'active' row is inserted with `parent_id`
pointing at the consumed one. Presenting an already-consumed token is treated
as a compromise signal — the V0 response is to revoke every active/consumed
token for the user (chain-only invalidation deferred to V0.5+, see
IDEAS_BACKLOG).

Atomicity: the lookup uses `SELECT ... FOR UPDATE` so two concurrent refresh
requests on the same token serialise; only one wins the rotation. Commit
boundary is the caller's — this service only stages writes via `db.add` /
`db.execute` and `db.flush()`.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.schema import RefreshToken


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RefreshTokenError(Exception):
    """Base for all refresh-token errors."""


class RefreshTokenNotFound(RefreshTokenError):
    """Token hash not in DB — never issued, or pruned."""


class RefreshTokenExpired(RefreshTokenError):
    """Token past `expires_at`. User must re-authenticate."""


class RefreshTokenAlreadyConsumed(RefreshTokenError):
    """REUSE DETECTED. Chain has been invalidated as a compromise response.

    Carries `user_id` (chain owner) and `revoked_count` (tokens nuked) so the
    caller can audit without a re-query. The chain invalidation is staged on
    the session — commit boundary stays with the caller so it can also stage
    an audit row in the same transaction.
    """

    def __init__(self, *, user_id: str, revoked_count: int) -> None:
        super().__init__(f"refresh token reuse: user_id={user_id}")
        self.user_id = user_id
        self.revoked_count = revoked_count


class RefreshTokenRevoked(RefreshTokenError):
    """Explicitly revoked. Possibly cascaded from a prior reuse hit, or
    revoked by an admin action (V0.5+)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_token() -> str:
    """Cryptographically secure URL-safe token, ~256 bits of entropy."""
    return secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    """SHA-256 hex digest used as the DB-side identifier for the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def issue_refresh_token(
    db: AsyncSession,
    *,
    user_id: str,
    parent_id: str | None = None,
) -> tuple[str, str]:
    """Issue a fresh refresh token.

    Returns `(token_plaintext, token_id)`. The plaintext is shown only once
    (returned here, sent to the client); the DB stores only its hash.

    `parent_id` is `None` on the initial issue (post-register, post-login)
    and the previous row's id during rotation.
    """
    token_plain = _generate_token()
    token_hash = _hash_token(token_plain)
    expires_at = datetime.utcnow() + timedelta(days=settings.refresh_token_ttl_days)

    row = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        parent_id=parent_id,
        status="active",
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()  # materialise the autoincrement id; commit boundary stays with caller
    return token_plain, row.id


async def consume_refresh_token(
    db: AsyncSession,
    token_plain: str,
) -> tuple[str, str, str]:
    """Atomic rotation: validate the presented token, mark it consumed, issue a new one.

    Returns `(new_token_plaintext, new_token_id, user_id)` — `user_id` is
    surfaced so the caller can mint a fresh access token without a second
    DB round-trip.

    Raises one of the typed `RefreshTokenError` subclasses. On
    `RefreshTokenAlreadyConsumed` the user's whole token set has already been
    revoked as part of the consumption path — caller still needs to commit
    so the revocation persists.
    """
    token_hash = _hash_token(token_plain)

    # Pessimistic lock: two concurrent refresh requests on the same token
    # serialise; only one wins the rotation, the other re-reads the now-consumed
    # row and falls into the reuse-detection path.
    stmt = (
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .with_for_update()
    )
    token = (await db.execute(stmt)).scalar_one_or_none()

    if token is None:
        raise RefreshTokenNotFound()

    # Status check order: revoked → consumed (reuse) → expired. Reuse beats
    # expiry on purpose — a leaked-token replay attempt after expiry is still a
    # compromise signal, not a routine "token expired" event.
    if token.status == "revoked":
        raise RefreshTokenRevoked()

    if token.status == "consumed":
        revoked_count = await _invalidate_user_tokens(db, user_id=token.user_id)
        raise RefreshTokenAlreadyConsumed(
            user_id=token.user_id, revoked_count=revoked_count
        )

    if token.expires_at < datetime.utcnow():
        raise RefreshTokenExpired()

    # Rotate: flip presented token to consumed, issue a fresh one with parent_id link.
    now = datetime.utcnow()
    token.status = "consumed"
    token.consumed_at = now

    new_plain, new_id = await issue_refresh_token(
        db, user_id=token.user_id, parent_id=token.id
    )
    return new_plain, new_id, token.user_id


async def _invalidate_user_tokens(db: AsyncSession, *, user_id: str) -> int:
    """Revoke every active/consumed refresh token for `user_id`.

    V0 simplification: blunt revoke-all on reuse detection. V0.5+ replaces
    this with a recursive CTE that walks the parent_id chain and revokes only
    the compromised branch — see IDEAS_BACKLOG. Returns the number of rows
    affected so the caller can log it on the security audit row.
    """
    result = await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.status.in_(["active", "consumed"]),
        )
        .values(status="revoked", consumed_at=datetime.utcnow())
    )
    return result.rowcount or 0
