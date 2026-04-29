"""V0 marketplace category vocabulary (brief task 4.1).

Closed list — adding a category in V0 is a code change. Categories are
the coarse axis of `find_matches` (4.3): two intents can only match if
they share a category, so the granularity here directly shapes how
loose vs. strict matching feels in practice.

Hard-forbidden categories (`adult`, `weapons`, ...) live in
`platform_limits.HARD_FORBIDDEN_CATEGORIES` — they are a *platform*
risk-posture decision, distinct from the *vocabulary* defined here.
The two are kept disjoint: `is_allowed` rejects forbidden categories
even though they're not in `V0_CATEGORIES`, and `is_forbidden` checks
the platform list independently — so a buggy client sending
`category="weapons"` gets rejected with the right error code regardless
of which check runs first.
"""
from __future__ import annotations

from typing import Final

from app.core import platform_limits as pl


V0_CATEGORIES: Final[tuple[str, ...]] = (
    # Electronics
    "electronics_laptops",
    "electronics_phones",
    "electronics_audio",
    "electronics_gaming",
    "electronics_components",
    # Fashion
    "fashion_clothing",
    "fashion_shoes",
    "fashion_accessories",
    "fashion_bags",
    # Home
    "home_furniture",
    "home_decor",
    "home_appliances",
    "home_kitchen",
    # Hobby & Sport
    "hobby_books",
    "hobby_music_instruments",
    "hobby_collectibles",
    "hobby_vinyls",
    "sport_bicycles",
    "sport_equipment",
    # Tools
    "tools_diy",
    "tools_garden",
    # Misc
    "misc_other",
)


def is_forbidden(category: str) -> bool:
    """True if the category is on the platform hard-forbidden list."""
    return category in pl.HARD_FORBIDDEN_CATEGORIES


def is_allowed(category: str) -> bool:
    """True iff the category is in V0_CATEGORIES and not hard-forbidden."""
    return category in V0_CATEGORIES and not is_forbidden(category)
