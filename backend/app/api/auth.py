"""Auth API routes — tier 0 onboarding (brief task 2.1, hardened at 7.1).

Rate limiting (7.1) — all keyed by client IP since callers are
unauthenticated at this stage:

  - register/begin, register/complete: `auth_strict` (5/min/IP) — anti
    enumeration, costliest path (DB write + WebAuthn options + future
    email side-effect)
  - login/begin, login/complete: `auth_normal` (10/min/IP) — UX-aware,
    legitimate users may retry on failed biometric prompt
  - refresh: `auth_refresh` (30/min/IP) — token rotation. Per-user
    keying isn't viable here: the refresh token sits in the request
    body, but slowapi's `key_func` is sync-only and consuming the
    body stream would break FastAPI's body parsing downstream. IP
    keying with a generous 30/min cap is the pragmatic call.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.services import auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterBeginRequest(BaseModel):
    email: EmailStr


class RegisterCompleteRequest(BaseModel):
    credential: dict[str, Any]
    challenge_token: str


class LoginBeginRequest(BaseModel):
    email: EmailStr


class LoginCompleteRequest(BaseModel):
    credential: dict[str, Any]
    challenge_token: str


class BeginResponse(BaseModel):
    options: dict[str, Any]
    challenge_token: str


class TokenResponse(BaseModel):
    user_id: str
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


def _to_http(exc: auth_service.AuthError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": str(exc)},
    )


@router.post("/register/begin", response_model=BeginResponse)
@limiter.limit(lambda: settings.rate_limit_auth_strict)
async def register_begin(
    request: Request,
    body: RegisterBeginRequest,
    db: AsyncSession = Depends(get_db),
) -> BeginResponse:
    try:
        options, token = await auth_service.begin_registration(db, email=body.email)
    except auth_service.AuthError as exc:
        raise _to_http(exc) from exc
    return BeginResponse(options=options, challenge_token=token)


@router.post("/register/complete", response_model=TokenResponse)
@limiter.limit(lambda: settings.rate_limit_auth_strict)
async def register_complete(
    request: Request,
    body: RegisterCompleteRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    actor_ip = request.client.host if request.client else None
    try:
        user_id, access, refresh = await auth_service.complete_registration(
            db,
            credential=body.credential,
            challenge_token=body.challenge_token,
            actor_ip=actor_ip,
        )
    except auth_service.AuthError as exc:
        raise _to_http(exc) from exc
    return TokenResponse(
        user_id=user_id, access_token=access, refresh_token=refresh
    )


@router.post("/login/begin", response_model=BeginResponse)
@limiter.limit(lambda: settings.rate_limit_auth_normal)
async def login_begin(
    request: Request,
    body: LoginBeginRequest,
    db: AsyncSession = Depends(get_db),
) -> BeginResponse:
    try:
        options, token = await auth_service.begin_login(db, email=body.email)
    except auth_service.AuthError as exc:
        raise _to_http(exc) from exc
    return BeginResponse(options=options, challenge_token=token)


@router.post("/login/complete", response_model=TokenResponse)
@limiter.limit(lambda: settings.rate_limit_auth_normal)
async def login_complete(
    request: Request,
    body: LoginCompleteRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    try:
        user_id, access, refresh = await auth_service.complete_login(
            db,
            credential=body.credential,
            challenge_token=body.challenge_token,
        )
    except auth_service.AuthError as exc:
        raise _to_http(exc) from exc
    return TokenResponse(
        user_id=user_id, access_token=access, refresh_token=refresh
    )


# ---------------------------------------------------------------------------
# Refresh access token (brief task 2.5)
# ---------------------------------------------------------------------------


class RefreshRequest(BaseModel):
    refresh_token: str


class RefreshResponse(BaseModel):
    access_token: str
    expires_in_seconds: int
    token_type: str = "bearer"


@router.post("/refresh", response_model=RefreshResponse)
@limiter.limit(lambda: settings.rate_limit_auth_refresh)
async def refresh(
    request: Request,
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    """Exchange a refresh token for a fresh access token.

    The new access token reflects the user's CURRENT tier (read from DB),
    not the tier embedded in the refresh JWT — so a user promoted via
    Self verification mid-session gets the right token without re-login.
    """
    try:
        new_access, ttl = await auth_service.refresh_access_token(
            db, refresh_token=body.refresh_token
        )
    except auth_service.AuthError as exc:
        raise _to_http(exc) from exc
    return RefreshResponse(access_token=new_access, expires_in_seconds=ttl)
