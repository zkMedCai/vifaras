"""Content moderation — validate user-generated text fields (brief 7.1.3).

V0 strategy: hardcoded blacklist + case-insensitive substring matching.
The trade-off is explicit: false positives (the classic "Scunthorpe
problem" — legitimate words containing a blacklisted substring) in
exchange for zero external dependencies and predictable behaviour. We
accept the FP rate for V0 and revisit at V0.5+ once alpha feedback
tells us whether it's a real UX issue.

V0.5+ refinement track (see IDEAS_BACKLOG):
  - Word-boundary regex to reduce FPs
  - Anthropic Moderation API for nuance / multilingual coverage
  - Per-tier policy (Tier 0 stricter than Tier 2)
  - Auto-blocking once accumulated AuditLog data calibrates thresholds

Integration is service-layer (not Pydantic boundary) so the agent
runtime — which composes intents/messages programmatically without
hitting the HTTP API — is bound by the same rules. That entry point
will be wired in [7.1.4].
"""
from __future__ import annotations


# ~25 V0 starter terms — most common Italian/English profanity plus a
# small slur set. Compact intentionally to keep the commit history
# readable; expand based on alpha feedback before / during V0.5+.
PROFANITY_BLACKLIST: frozenset[str] = frozenset(
    {
        # English profanity
        "fuck",
        "shit",
        "asshole",
        "bitch",
        "bastard",
        "cunt",
        "dick",
        "piss",
        "whore",
        "slut",
        # Italian profanity
        "cazzo",
        "merda",
        "stronzo",
        "vaffanculo",
        "puttana",
        "troia",
        "coglione",
        "porco",
        "bastardo",
        "fottiti",
        # Slurs — V0 starter set, not exhaustive.
        "nigger",
        "faggot",
        "frocio",
        "negro",
        "checca",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ModerationError(Exception):
    """Base — user-generated text failed policy.

    Mirrors the project's typed-service-error convention: `code` and
    `http_status` are class attributes (per subclass), `field` is
    per-instance so the API layer can surface which field was rejected
    without parsing the message. API detail envelope is
    `{code, message, field}`, matching the post-7.0 shape.
    """

    code: str = "moderation_error"
    http_status: int = 422

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


class EmptyAfterStrip(ModerationError):
    code = "empty_after_strip"


class TooLong(ModerationError):
    code = "too_long"


class ProfanityDetected(ModerationError):
    code = "profanity_detected"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def contains_profanity(text: str) -> bool:
    """Case-insensitive substring match against PROFANITY_BLACKLIST.

    V0 substring choice over word-boundary regex is deliberate — see
    module docstring for the trade-off rationale. The Scunthorpe
    problem applies: 'cunt' will flag 'Scunthorpe', 'shit' will flag
    'shitake'. Accepted until V0.5+.
    """
    text_lower = text.lower()
    return any(word in text_lower for word in PROFANITY_BLACKLIST)


def moderate_text(text: str, field_name: str, max_length: int) -> None:
    """Validate `text` against V0 content rules, raise on rejection.

    Order: empty-after-strip → length → profanity. First failure wins;
    subsequent rules are not evaluated. Length is checked against the
    stripped form, so leading/trailing whitespace doesn't count toward
    the cap.

    The function does NOT mutate `text` — callers persist the original
    or stripped form per their schema. It only validates.
    """
    stripped = text.strip()
    if not stripped:
        raise EmptyAfterStrip(
            f"{field_name} cannot be empty",
            field=field_name,
        )
    if len(stripped) > max_length:
        raise TooLong(
            f"{field_name} exceeds {max_length} characters",
            field=field_name,
        )
    if contains_profanity(stripped):
        raise ProfanityDetected(
            "Content contains inappropriate language",
            field=field_name,
        )


def moderate_optional(
    text: str | None, field_name: str, max_length: int
) -> None:
    """Variant of `moderate_text` that no-ops on `None`.

    Convenience for partial-update flows (e.g. `update_intent`) and
    optional request fields where "absent" must NOT raise but "present
    and dirty" must. `None` and "field not supplied" map to the same
    policy here. Empty/whitespace-only strings still raise via
    `EmptyAfterStrip` — moderate_optional is about presence, not
    content.
    """
    if text is None:
        return
    moderate_text(text, field_name, max_length)
