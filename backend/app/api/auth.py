"""Auth API routes — tier 0 onboarding (brief task 2.1)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
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
async def register_begin(
    body: RegisterBeginRequest,
    db: AsyncSession = Depends(get_db),
) -> BeginResponse:
    try:
        options, token = await auth_service.begin_registration(db, email=body.email)
    except auth_service.AuthError as exc:
        raise _to_http(exc) from exc
    return BeginResponse(options=options, challenge_token=token)


@router.post("/register/complete", response_model=TokenResponse)
async def register_complete(
    body: RegisterCompleteRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    try:
        user_id, access, refresh = await auth_service.complete_registration(
            db,
            credential=body.credential,
            challenge_token=body.challenge_token,
        )
    except auth_service.AuthError as exc:
        raise _to_http(exc) from exc
    return TokenResponse(
        user_id=user_id, access_token=access, refresh_token=refresh
    )


@router.post("/login/begin", response_model=BeginResponse)
async def login_begin(
    body: LoginBeginRequest,
    db: AsyncSession = Depends(get_db),
) -> BeginResponse:
    try:
        options, token = await auth_service.begin_login(db, email=body.email)
    except auth_service.AuthError as exc:
        raise _to_http(exc) from exc
    return BeginResponse(options=options, challenge_token=token)


@router.post("/login/complete", response_model=TokenResponse)
async def login_complete(
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
