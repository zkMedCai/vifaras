"""Content moderation service unit tests (brief 7.1.3).

12 unit tests covering empty/length/profanity rules, case-insensitivity,
substring-match trade-off (Scunthorpe), unicode safety, and the typed-
error envelope. No DB, no HTTP — that's [7.1.4].
"""
from __future__ import annotations

import pytest

from app.services.content_moderation import (
    EmptyAfterStrip,
    ProfanityDetected,
    TooLong,
    moderate_optional,
    moderate_text,
)


def test_moderate_text_rejects_empty_after_strip():
    with pytest.raises(EmptyAfterStrip) as exc:
        moderate_text("   ", "title", 100)
    assert exc.value.code == "empty_after_strip"
    assert exc.value.field == "title"
    assert exc.value.http_status == 422


def test_moderate_text_rejects_text_over_length():
    with pytest.raises(TooLong) as exc:
        moderate_text("a" * 101, "title", 100)
    assert exc.value.code == "too_long"
    assert exc.value.field == "title"


def test_moderate_text_accepts_text_at_exact_length():
    """Length check is `len > max_length`, so the boundary value passes."""
    moderate_text("a" * 100, "title", 100)


def test_moderate_text_strips_whitespace_before_length_check():
    """'  hello  '.strip() == 'hello' (5 chars) — well under the cap."""
    moderate_text("  hello  ", "title", 100)


def test_moderate_text_rejects_english_profanity():
    with pytest.raises(ProfanityDetected) as exc:
        moderate_text("This is shit", "description", 100)
    assert exc.value.code == "profanity_detected"
    assert exc.value.field == "description"


def test_moderate_text_rejects_italian_profanity():
    with pytest.raises(ProfanityDetected):
        moderate_text("Che cazzo dici", "description", 100)


def test_moderate_text_profanity_is_case_insensitive():
    with pytest.raises(ProfanityDetected):
        moderate_text("FUCK this", "description", 100)


def test_moderate_text_substring_match_scunthorpe_problem():
    """V0 substring match flags 'Scunthorpe' (false positive on 'cunt').

    Documented trade-off — locked in by this test so accidental migration
    to word-boundary regex without a deliberate decision will fail here."""
    with pytest.raises(ProfanityDetected):
        moderate_text("Scunthorpe", "title", 100)


def test_moderate_text_accepts_clean_text():
    moderate_text("Looking for a vintage denim jacket", "title", 100)


def test_moderate_text_emoji_is_safe():
    moderate_text("emoji 🎉 test", "description", 100)


def test_moderate_text_multiple_profanity_words_any_match_rejects():
    with pytest.raises(ProfanityDetected):
        moderate_text("shit and fuck", "description", 100)


def test_moderation_error_field_attribute_survives_raise():
    """Direct-construction sanity: the typed-error shape API callers
    expect — `code`, `field`, `http_status` — is reachable on the
    instance, not just embedded in the str message."""
    err = ProfanityDetected("inappropriate language", field="description")
    assert err.field == "description"
    assert err.code == "profanity_detected"
    assert err.http_status == 422
    assert str(err) == "inappropriate language"


def test_moderate_optional_skips_none():
    """The partial-update helper must no-op on None and delegate on str.

    Locks the contract: `None` is "field not supplied" (skip), empty
    string is "field supplied empty" (raise). Without this test the
    helper's None branch is uncovered."""
    # None → no exception
    moderate_optional(None, "description", 100)
    # Whitespace-only str → still raises (delegates to moderate_text)
    with pytest.raises(EmptyAfterStrip):
        moderate_optional("   ", "description", 100)
    # Profane str → still raises
    with pytest.raises(ProfanityDetected):
        moderate_optional("shit", "description", 100)
