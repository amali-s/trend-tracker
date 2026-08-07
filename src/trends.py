"""Sector rollup and four-week deltas.

Pure computation — nothing here reads or writes a file. `build_digest` takes
this week's investments plus an already-loaded history dict and returns a
`WeeklyDigest`. `main.py` owns the I/O:

    history = state.load_weekly_history()
    digest  = trends.build_digest(investments, history, week_ending)
    state.save_week(week_ending.isoformat(), investments)

Keeping `state.py` the sole writer of `weekly_history.json` avoids two modules
with different ideas about the file's shape.

Note on layout: PLAN §1 lists `WeeklyDigest` under `models.py`. It lives here
instead, because it has to reference `SectorRow` / `StageRow` / `Mover`, and
splitting those across the two modules would mean `models.py` importing
`trends.py` while `trends.py` imports `models.py`. `models.py` stays the
scraped-and-extracted entities; this module owns the computed rollup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .models import Investment, SourceSummary
from .sectors import DEFAULT_SECTOR
from .weekrange import subject_line, week_label

# How many prior weeks feed the comparison.
BASELINE_WEEKS = 4

# How many sectors to surface in each direction.
MOVER_LIMIT = 3

# Raw round stages collapse into these for the mix. Series A through J read as
# noise at this granularity — the question the mix answers is how early the
# week's money went, not which specific letter.
#
# Order is the display order: earliest stage first, so the row reads as a
# progression rather than a leaderboard.
STAGE_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Pre-Seed & Seed", ("Pre-Seed", "Seed")),
    ("Series A", ("Series A",)),
    ("Series B", ("Series B",)),
    ("Series C+ & Growth", (
        "Series C", "Series D", "Series E", "Series F",
        "Series G", "Series H", "Series I", "Series J", "Growth",
    )),
    ("Other", ("Bridge", "Extension", "Unknown")),
)

_STAGE_TO_BUCKET = {
    stage: label for label, stages in STAGE_BUCKETS for stage in stages
}
_BUCKET_ORDER = [label for label, _ in STAGE_BUCKETS]

OTHER_BUCKET = "Other"


def bucket_stage(stage: str) -> str:
    """Collapse a round stage into its display bucket."""
    return _STAGE_TO_BUCKET.get((stage or "").strip(), OTHER_BUCKET)


def format_usd(amount: Optional[float]) -> str:
    """Render a dollar figure compactly: $1.2B, $450M, $4.5M, $750K.

    `None` is the undisclosed case and renders as a word, not a zero — a round
    with no stated size is not a round of nothing.
    """
    if amount is None:
        return "Undisclosed"

    sign = "-" if amount < 0 else ""
    value = abs(float(amount))

    if value >= 1_000_000_000:
        text = f"{value / 1_000_000_000:.1f}B".replace(".0B", "B")
    elif value >= 10_000_000:
        text = f"{value / 1_000_000:.0f}M"
    elif value >= 1_000_000:
        text = f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    elif value >= 1_000:
        text = f"{value / 1_000:.0f}K"
    else:
        text = f"{value:,.0f}"

    return f"{sign}${text}"


def _share(part: float, whole: float) -> float:
    """Percentage of the week's capital, safe when the week has none."""
    return (part / whole * 100.0) if whole else 0.0


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class Headline:
    """The three numbers at the top of the email."""

    total_usd: int = 0
    deal_count: int = 0
    disclosed_count: int = 0
    undisclosed_count: int = 0
    firms_active: int = 0
    largest: Optional[Investment] = None


@dataclass
class SectorRow:
    sector: str
    deals: int = 0
    total_usd: int = 0
    undisclosed: int = 0
    share_pct: float = 0.0
    # None until at least one prior week exists — a missing baseline has to be
    # distinguishable from a flat one, or the first email reads as "no change"
    # when it means "nothing to compare against".
    avg_4w_usd: Optional[float] = None
    delta_usd: Optional[float] = None
    delta_pct: Optional[float] = None


@dataclass
class StageRow:
    stage: str
    deals: int = 0
    total_usd: int = 0
    share_pct: float = 0.0


@dataclass
class Mover:
    sector: str
    delta_usd: float
    delta_pct: Optional[float]
    direction: str  # "up" | "down"


@dataclass
class FirmActivity:
    firm: str
    deals: int
    total_usd: int


@dataclass
class WeeklyDigest:
    week_ending: date
    week_label: str
    subject: str
    headline: Headline
    sectors: list[SectorRow] = field(default_factory=list)
    stages: list[StageRow] = field(default_factory=list)
    most_active_firm: Optional[FirmActivity] = None
    movers_up: list[Mover] = field(default_factory=list)
    movers_down: list[Mover] = field(default_factory=list)
    investments: list[Investment] = field(default_factory=list)
    source_summaries: list[SourceSummary] = field(default_factory=list)
    # Number of prior weeks that fed the average. 0 means every delta is None
    # and the email should say so rather than render a comparison.
    baseline_weeks: int = 0

    @property
    def has_baseline(self) -> bool:
        return self.baseline_weeks > 0

    @property
    def is_empty(self) -> bool:
        return not self.investments

    @property
    def unhealthy_sources(self) -> list[SourceSummary]:
        """Sources that errored or returned nothing — flagged in the footer."""
        return [s for s in self.source_summaries if not s.healthy]


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def baseline_weeks(history: dict, week_ending: date, weeks: int = BASELINE_WEEKS) -> list[str]:
    """The up-to-N history keys strictly before `week_ending`, oldest first.

    The current week is excluded unconditionally, whether or not it has
    already been written. `state.save_week()` keys on the same date, so if
    `main.py` ever saved before computing trends the week would land in its
    own baseline and pull every delta toward zero. Excluding it here makes the
    call order irrelevant rather than load-bearing.
    """
    key = week_ending.isoformat()
    prior = sorted(k for k in history if k < key)
    return prior[-weeks:]


def sector_average(history: dict, weeks: list[str], sector: str) -> Optional[float]:
    """Mean weekly capital into `sector` across the baseline weeks.

    A sector absent from a week counts as **$0 for that week**, not as a week
    to skip. Otherwise a sector that took $100M once and nothing since would
    report a $100M average and a zero delta, hiding exactly the fall the
    comparison exists to show.
    """
    if not weeks:
        return None
    total = sum(
        history.get(w, {}).get("by_sector", {}).get(sector, {}).get("total_usd", 0) or 0
        for w in weeks
    )
    return total / len(weeks)


def _baseline_sectors(history: dict, weeks: list[str]) -> set[str]:
    """Sectors that saw any activity in the baseline window."""
    seen: set[str] = set()
    for week in weeks:
        for sector, stats in history.get(week, {}).get("by_sector", {}).items():
            if (stats.get("total_usd") or 0) or (stats.get("deals") or 0):
                seen.add(sector)
    return seen


# ---------------------------------------------------------------------------
# Rollups
# ---------------------------------------------------------------------------

def build_headline(investments: list[Investment]) -> Headline:
    disclosed = [i for i in investments if i.funding_amount_usd is not None]
    firms = {firm for i in investments for firm in i.vc_firms}
    return Headline(
        total_usd=sum(i.funding_amount_usd or 0 for i in investments),
        deal_count=len(investments),
        disclosed_count=len(disclosed),
        undisclosed_count=len(investments) - len(disclosed),
        firms_active=len(firms),
        largest=max(disclosed, key=lambda i: i.funding_amount_usd) if disclosed else None,
    )


def build_sector_rows(
    investments: list[Investment], history: dict, weeks: list[str], total_usd: int
) -> list[SectorRow]:
    """Per-sector totals with the delta against the four-week average.

    Sectors with no deals this week are included when they appear in the
    baseline — a sector that took money recently and none now is a real
    down-mover and should be visible rather than silently dropping out.
    """
    rows: dict[str, SectorRow] = {}

    for inv in investments:
        sector = inv.sector or DEFAULT_SECTOR
        row = rows.setdefault(sector, SectorRow(sector=sector))
        row.deals += 1
        if inv.funding_amount_usd is None:
            row.undisclosed += 1
        else:
            row.total_usd += inv.funding_amount_usd

    for sector in _baseline_sectors(history, weeks):
        rows.setdefault(sector, SectorRow(sector=sector))

    for row in rows.values():
        row.share_pct = _share(row.total_usd, total_usd)
        average = sector_average(history, weeks, row.sector)
        row.avg_4w_usd = average
        if average is not None:
            row.delta_usd = row.total_usd - average
            row.delta_pct = (row.delta_usd / average * 100.0) if average else None

    return sorted(
        rows.values(),
        key=lambda r: (-r.total_usd, -r.deals, r.sector),
    )


def build_stage_rows(investments: list[Investment], total_usd: int) -> list[StageRow]:
    """Stage mix, in early-to-late order. Empty buckets are omitted."""
    rows: dict[str, StageRow] = {}

    for inv in investments:
        label = bucket_stage(inv.round_stage)
        row = rows.setdefault(label, StageRow(stage=label))
        row.deals += 1
        row.total_usd += inv.funding_amount_usd or 0

    for row in rows.values():
        row.share_pct = _share(row.total_usd, total_usd)

    return [rows[label] for label in _BUCKET_ORDER if label in rows]


def most_active_firm(investments: list[Investment]) -> Optional[FirmActivity]:
    """The firm in the most rounds this week.

    A syndicated round counts for every firm in it, which is the right
    reading — all of them were active in that deal.
    """
    tally: dict[str, list[int]] = {}
    for inv in investments:
        for firm in inv.vc_firms:
            entry = tally.setdefault(firm, [0, 0])
            entry[0] += 1
            entry[1] += inv.funding_amount_usd or 0

    if not tally:
        return None

    firm, (deals, total) = max(
        tally.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0])
    )
    return FirmActivity(firm=firm, deals=deals, total_usd=total)


def build_movers(rows: list[SectorRow], limit: int = MOVER_LIMIT) -> tuple[list[Mover], list[Mover]]:
    """Sectors that moved most against the four-week average.

    Ranked by **absolute dollar change**, with the percentage reported
    alongside. Ranking by percentage would make every $0-to-anything sector
    infinite and float a $2M blip above a $200M shift.
    """
    movers = [
        Mover(
            sector=r.sector,
            delta_usd=r.delta_usd,
            delta_pct=r.delta_pct,
            direction="up" if r.delta_usd > 0 else "down",
        )
        for r in rows
        if r.delta_usd is not None and r.delta_usd != 0
    ]

    up = sorted([m for m in movers if m.direction == "up"],
                key=lambda m: (-m.delta_usd, m.sector))[:limit]
    down = sorted([m for m in movers if m.direction == "down"],
                  key=lambda m: (m.delta_usd, m.sector))[:limit]
    return up, down


# ---------------------------------------------------------------------------

def build_digest(
    investments: list[Investment],
    history: dict,
    week_ending: date,
    source_summaries: Optional[list[SourceSummary]] = None,
) -> WeeklyDigest:
    """Assemble the whole rollup for one week.

    Safe on an empty week: every total is zero, no division happens, and the
    digest still carries the source summaries so the "no new activity" email
    can show its per-source table.
    """
    investments = list(investments)
    weeks = baseline_weeks(history, week_ending)

    headline = build_headline(investments)
    sectors = build_sector_rows(investments, history, weeks, headline.total_usd)
    stages = build_stage_rows(investments, headline.total_usd)
    up, down = build_movers(sectors)

    return WeeklyDigest(
        week_ending=week_ending,
        week_label=week_label(week_ending),
        subject=subject_line(week_ending),
        headline=headline,
        sectors=sectors,
        stages=stages,
        most_active_firm=most_active_firm(investments),
        movers_up=up,
        movers_down=down,
        investments=investments,
        source_summaries=list(source_summaries or []),
        baseline_weeks=len(weeks),
    )
