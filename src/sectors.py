"""The fixed sector taxonomy.

This list is a **closed enum**, and that is the whole point. Requirement two is
understanding where money is going over time, and that only works if the label
is stable across weeks. If Claude free-forms the sector, week 1 says "AI
infrastructure", week 2 says "ML platform tooling", and nothing is comparable —
the trend view silently becomes noise.

So the extraction schema constrains `sector` to exactly these values (JSON
Schema `enum`, enforced by the API rather than requested in the prompt), and
anything unrecognized falls back to `Other`.

Changing this list invalidates the trend history in `data/weekly_history.json`,
because a sector that gets renamed reads as one sector disappearing and another
appearing from nothing. Edit it before the history is worth keeping, or accept
a discontinuity and note the week it happened.

`sub_sector` on `Investment` is free text and exists for colour on the card.
Never trend on it.
"""

from __future__ import annotations

DEFAULT_SECTOR = "Other"

# Order matters only for display; the enum itself is unordered.
SECTORS: tuple[str, ...] = (
    "AI Infrastructure",
    "AI Applications",
    "Developer Tools",
    "Fintech",
    "Healthcare & Bio",
    "Enterprise SaaS",
    "Security",
    "Consumer",
    "Robotics & Hardware",
    "Climate & Energy",
    "Defense & Gov",
    "Commerce & Retail",
    "Data & Analytics",
    "Crypto",
    DEFAULT_SECTOR,
)

# Case-insensitive lookup, so a model that returns "fintech" or "AI
# applications" still lands on the canonical spelling rather than in Other.
_BY_LOWER = {s.lower(): s for s in SECTORS}


def normalize_sector(value: str | None) -> str:
    """Map a returned sector onto the enum, falling back to `Other`.

    The schema already constrains the model to these values, so this is a
    second line of defence — it also covers the extraction cache, which may
    hold rows written before a taxonomy edit.
    """
    if not value:
        return DEFAULT_SECTOR
    return _BY_LOWER.get(value.strip().lower(), DEFAULT_SECTOR)
