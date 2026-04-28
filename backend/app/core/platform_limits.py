"""Platform-wide hard ceilings + V0-fixed mandate vocabulary (brief task 2.4).

These are the non-negotiable limits enforced by the server regardless of
what the user (or a malicious client) asks for in the mandate draft. They
live here, NOT in `core/config.py` (per DQ-5: "config morta è debt"),
because they're invariants of the platform's risk posture, not deployment
config.

Tightening one of these is a code change + new release. Loosening it for
V1 is intentional, deliberate, reviewed.
"""
from __future__ import annotations

from typing import Final


# ---------------------------------------------------------------------------
# Hard caps the user CANNOT exceed (server-side rejection in /draft)
# ---------------------------------------------------------------------------


MAX_PRICE_PER_DEAL_EUR: Final[int] = 1000
MAX_TOTAL_VOLUME_EUR_PER_MANDATE: Final[int] = 5000
MAX_TOTAL_VOLUME_EUR_PER_DAY: Final[int] = 1000
MAX_DEALS_PER_DAY: Final[int] = 10
MAX_ACTIVE_INTENTS: Final[int] = 20
MAX_CONCURRENT_NEGOTIATIONS: Final[int] = 10
MAX_MANDATE_DURATION_DAYS: Final[int] = 90


# ---------------------------------------------------------------------------
# V0 default mandate values (returned by /draft when user omits)
# ---------------------------------------------------------------------------


DEFAULT_MAX_PRICE_PER_DEAL_EUR: Final[int] = 100
DEFAULT_MAX_TOTAL_VOLUME_EUR_PER_MANDATE: Final[int] = 500
DEFAULT_MAX_TOTAL_VOLUME_EUR_PER_DAY: Final[int] = 200
DEFAULT_MAX_DEALS_PER_DAY: Final[int] = 3
DEFAULT_MAX_ACTIVE_INTENTS: Final[int] = 10
DEFAULT_MAX_CONCURRENT_NEGOTIATIONS: Final[int] = 5
DEFAULT_MANDATE_DURATION_DAYS: Final[int] = 30


# ---------------------------------------------------------------------------
# Geo & categories (V0 = Italy only)
# ---------------------------------------------------------------------------


GEO_SCOPE_V0: Final[tuple[str, ...]] = ("IT",)


# Hard-forbidden categories: a user cannot whitelist these in any mandate.
# Adding a category here is a "we say no" decision; removing one is a
# regulatory / policy change.
HARD_FORBIDDEN_CATEGORIES: Final[tuple[str, ...]] = (
    "adult",
    "weapons",
    "alcohol",
    "drugs",
    "nft_crypto",
    "pharmaceuticals",
    "tobacco",
)


# ---------------------------------------------------------------------------
# V0 fixed mandate vocabulary
# ---------------------------------------------------------------------------


# Actions an agent under any V0 mandate is allowed to take. Closed list:
# new actions need a code change. Aligns with `agents/tool_layer.py`.
V0_DEFAULT_ALLOWED_ACTIONS: Final[tuple[str, ...]] = (
    "create_intent",
    "search_intents",
    "send_offer",
    "send_counter_offer",
    "accept_offer",
    "reject_offer",
    "send_message",
    "read_inbox",
    "check_state",
)


# Always forbidden, even if a buggy client sends them in a draft.
V0_DEFAULT_FORBIDDEN_ACTIONS: Final[tuple[str, ...]] = (
    "modify_reservation_price",
    "delete_account",
)


# Step-up triggers: actions that need an explicit user passkey signature
# above the listed threshold (or always, if `always=True`).
V0_DEFAULT_STEP_UP_REQUIRED_FOR: Final[tuple[dict, ...]] = (
    {"action": "accept_offer", "above_eur": 100},
    {"action": "create_intent", "above_eur": 150},
    {"action": "modify_reservation_price", "always": True},
)


# Operating hours — V0 is 24/7. The mandate verifier reads this string;
# anything other than "24/7" is undefined behavior in V0 (parse it later
# if we add quiet hours).
V0_DEFAULT_OPERATING_HOURS: Final[str] = "24/7"


# Categories: V0 allows everything except the hard-forbidden list.
# `["*"]` is the wildcard for "all categories".
V0_DEFAULT_CATEGORIES_ALLOWED: Final[tuple[str, ...]] = ("*",)


# ---------------------------------------------------------------------------
# Revocation policy (V0 fixed)
# ---------------------------------------------------------------------------


REVOCATION_POLICY_V0: Final[dict] = {
    "revocable_anytime_by_principal": True,
    "auto_revoke_on_inactivity_days": 30,
    "auto_revoke_on_suspicious_pattern": True,
}


# ---------------------------------------------------------------------------
# Mandate spec version
# ---------------------------------------------------------------------------


MANDATE_SPEC_VERSION: Final[str] = "1.0"
