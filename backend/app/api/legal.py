"""Legal documentation endpoints ([7.4.4]).

V0: serves the static markdown from `docs/PRIVACY_POLICY.md` plus a small
metadata endpoint that mirrors the policy header. Public, no auth — GDPR
transparency requires a prospective user to read the policy before signing
up.

V0.5+: DB-backed versioning + per-user acceptance log + structured policy
diff between versions (entry in IDEAS_BACKLOG).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel


router = APIRouter(prefix="/api/legal", tags=["legal"])


# backend/app/api/legal.py → 4× parent → project root (where docs/ lives).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_PRIVACY_POLICY_PATH = _PROJECT_ROOT / "docs" / "PRIVACY_POLICY.md"


class PrivacyVersionResponse(BaseModel):
    version: str
    effective_date: str
    language: str


@router.get(
    "/privacy",
    response_class=PlainTextResponse,
    summary="Current privacy policy (markdown text)",
)
async def privacy_policy() -> str:
    """Return the current privacy policy as raw markdown.

    Public — no authentication required (GDPR transparency principle:
    prospective users must be able to read the policy pre-registration).
    Client renders the markdown.
    """
    if not _PRIVACY_POLICY_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail="Privacy policy file not found.",
        )
    return _PRIVACY_POLICY_PATH.read_text(encoding="utf-8")


@router.get(
    "/privacy/version",
    response_model=PrivacyVersionResponse,
    summary="Privacy policy version metadata",
)
async def privacy_version() -> PrivacyVersionResponse:
    """Return version metadata for the current privacy policy.

    V0: hardcoded to mirror the disclaimer header in
    `docs/PRIVACY_POLICY.md`. The `effective_date` stays "TBD-pre-launch"
    deliberately — flipping it requires both legal sign-off and a real
    publication date.
    """
    return PrivacyVersionResponse(
        version="1.0.0",
        effective_date="TBD-pre-launch",
        language="it",
    )
