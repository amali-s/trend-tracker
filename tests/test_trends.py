"""Tests for the sector rollup and the four-week comparison.

Pure functions with no I/O, so these can afford to be exhaustive. The one that
matters most is `test_sector_totals_equal_the_headline` — PLAN §11 calls for
asserting that per-sector dollar sums equal the headline total, because a
rollup that quietly loses money still renders as a perfectly nice email.
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import BlogPost, Investment, SourceSummary  # noqa: E402
from src.trends import (  # noqa: E402
    BASELINE_WEEKS,
    STAGE_BUCKETS,
    baseline_weeks,
    bucket_stage,
    build_digest,
    build_headline,
    build_movers,
    build_sector_rows,
    build_stage_rows,
    format_usd,
    most_active_firm,
    sector_average,
)

WEEK = date(2026, 8, 8)  # a Saturday — the spec week, August 2-8 2026


def inv(
    name: str = "Acme",
    usd: int | None = 30_000_000,
    sector: str = "AI Infrastructure",
    stage: str = "Series B",
    firms: list[str] | None = None,
) -> Investment:
    return Investment(
        company_name=name,
        funding_amount_usd=usd,
        funding_amount_raw="Undisclosed" if usd is None else f"${usd:,}",
        sector=sector,
        round_stage=stage,
        vc_firms=list(firms or ["Greylock"]),
        source_posts=[BlogPost(
            url=f"https://example.com/{name.lower()}", title=name, vc_firm=(firms or ["Greylock"])[0]
        )],
    )


def history_week(**sectors) -> dict:
    """A history entry shaped the way state.save_week() writes it."""
    by_sector = {
        sector: {"deals": 1, "total_usd": total, "undisclosed": 0}
        for sector, total in sectors.items()
    }
    return {
        "deals": len(by_sector),
        "total_usd": sum(sectors.values()),
        "by_sector": by_sector,
        "investments": [],
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

class TestFormatUSD:
    @pytest.mark.parametrize("amount,expected", [
        (1_200_000_000, "$1.2B"),
        (2_000_000_000, "$2B"),
        (450_000_000, "$450M"),
        (30_000_000, "$30M"),
        (4_500_000, "$4.5M"),
        (1_000_000, "$1M"),
        (750_000, "$750K"),
        (500, "$500"),
        (0, "$0"),
        (-5_000_000, "-$5M"),
    ])
    def test_renders_compactly(self, amount, expected):
        assert format_usd(amount) == expected

    def test_none_is_undisclosed_not_zero(self):
        """A round with no stated size is not a round of nothing."""
        assert format_usd(None) == "Undisclosed"


# ---------------------------------------------------------------------------
# Headline
# ---------------------------------------------------------------------------

class TestHeadline:
    def test_counts_and_totals(self):
        h = build_headline([
            inv("Acme", 30_000_000),
            inv("Beta", 20_000_000, firms=["Sequoia"]),
            inv("Gamma", None),
        ])
        assert h.total_usd == 50_000_000
        assert h.deal_count == 3
        assert h.disclosed_count == 2
        assert h.undisclosed_count == 1
        assert h.firms_active == 2

    def test_undisclosed_counts_as_deal_flow_but_not_dollars(self):
        h = build_headline([inv("Acme", None), inv("Beta", None)])
        assert h.deal_count == 2
        assert h.total_usd == 0
        assert h.undisclosed_count == 2

    def test_largest_ignores_undisclosed(self):
        h = build_headline([inv("Acme", 10_000_000), inv("Big", None)])
        assert h.largest.company_name == "Acme"

    def test_syndicate_counts_every_firm_as_active(self):
        h = build_headline([inv("Acme", 30_000_000, firms=["Sequoia", "Index Ventures"])])
        assert h.firms_active == 2

    def test_empty_week(self):
        h = build_headline([])
        assert (h.total_usd, h.deal_count, h.firms_active) == (0, 0, 0)
        assert h.largest is None


# ---------------------------------------------------------------------------
# The baseline window
# ---------------------------------------------------------------------------

class TestBaselineWeeks:
    def test_takes_the_four_most_recent_prior_weeks(self):
        history = {
            "2026-07-04": {}, "2026-07-11": {}, "2026-07-18": {},
            "2026-07-25": {}, "2026-08-01": {},
        }
        assert baseline_weeks(history, WEEK) == [
            "2026-07-11", "2026-07-18", "2026-07-25", "2026-08-01",
        ]

    def test_excludes_the_current_week_even_when_already_saved(self):
        """save_week() keys on the same date; the week must not seed its own
        baseline or every delta collapses toward zero."""
        history = {"2026-08-01": {}, "2026-08-08": {}}
        assert baseline_weeks(history, WEEK) == ["2026-08-01"]

    def test_ignores_weeks_after_the_current_one(self):
        history = {"2026-08-01": {}, "2026-08-15": {}}
        assert baseline_weeks(history, WEEK) == ["2026-08-01"]

    def test_cold_start_has_no_baseline(self):
        assert baseline_weeks({}, WEEK) == []

    def test_partial_history_is_used_as_is(self):
        history = {"2026-08-01": {}, "2026-07-25": {}}
        assert len(baseline_weeks(history, WEEK)) == 2


class TestSectorAverage:
    def test_averages_across_the_window(self):
        history = {
            "2026-07-25": history_week(**{"Fintech": 100_000_000}),
            "2026-08-01": history_week(**{"Fintech": 200_000_000}),
        }
        weeks = ["2026-07-25", "2026-08-01"]
        assert sector_average(history, weeks, "Fintech") == 150_000_000

    def test_a_missing_sector_counts_as_zero_for_that_week(self):
        """Otherwise a sector that took $100M once and nothing since would
        report a $100M average and hide the fall."""
        history = {
            "2026-07-11": history_week(**{"Crypto": 100_000_000}),
            "2026-07-18": history_week(**{"Fintech": 5_000_000}),
            "2026-07-25": history_week(**{"Fintech": 5_000_000}),
            "2026-08-01": history_week(**{"Fintech": 5_000_000}),
        }
        weeks = list(history)
        assert sector_average(history, weeks, "Crypto") == 25_000_000

    def test_no_baseline_returns_none_not_zero(self):
        assert sector_average({}, [], "Fintech") is None


# ---------------------------------------------------------------------------
# Sector table
# ---------------------------------------------------------------------------

class TestSectorRows:
    def test_sector_totals_equal_the_headline(self):
        """PLAN §11: per-sector sums must reconcile with the headline total."""
        investments = [
            inv("Acme", 30_000_000, sector="AI Infrastructure"),
            inv("Beta", 20_000_000, sector="Fintech"),
            inv("Gamma", 5_000_000, sector="Fintech"),
            inv("Delta", None, sector="Security"),
        ]
        headline = build_headline(investments)
        rows = build_sector_rows(investments, {}, [], headline.total_usd)
        assert sum(r.total_usd for r in rows) == headline.total_usd
        assert sum(r.deals for r in rows) == headline.deal_count
        assert sum(r.undisclosed for r in rows) == headline.undisclosed_count

    def test_undisclosed_excluded_from_sums_present_in_counts(self):
        investments = [inv("Acme", None, sector="Security")]
        rows = build_sector_rows(investments, {}, [], 0)
        row = next(r for r in rows if r.sector == "Security")
        assert row.deals == 1
        assert row.undisclosed == 1
        assert row.total_usd == 0

    def test_shares_sum_to_one_hundred(self):
        investments = [
            inv("Acme", 30_000_000, sector="AI Infrastructure"),
            inv("Beta", 10_000_000, sector="Fintech"),
        ]
        rows = build_sector_rows(investments, {}, [], 40_000_000)
        assert sum(r.share_pct for r in rows) == pytest.approx(100.0)

    def test_all_undisclosed_week_does_not_divide_by_zero(self):
        rows = build_sector_rows([inv("Acme", None)], {}, [], 0)
        assert rows[0].share_pct == 0.0

    def test_sorted_by_capital_then_deals(self):
        investments = [
            inv("Small", 1_000_000, sector="Consumer"),
            inv("Big", 90_000_000, sector="Fintech"),
        ]
        rows = build_sector_rows(investments, {}, [], 91_000_000)
        assert [r.sector for r in rows] == ["Fintech", "Consumer"]

    def test_cold_start_leaves_deltas_none(self):
        rows = build_sector_rows([inv("Acme", 30_000_000)], {}, [], 30_000_000)
        assert rows[0].avg_4w_usd is None
        assert rows[0].delta_usd is None
        assert rows[0].delta_pct is None

    def test_delta_against_the_average(self):
        history = {"2026-08-01": history_week(**{"AI Infrastructure": 10_000_000})}
        weeks = ["2026-08-01"]
        rows = build_sector_rows(
            [inv("Acme", 30_000_000, sector="AI Infrastructure")],
            history, weeks, 30_000_000,
        )
        row = rows[0]
        assert row.avg_4w_usd == 10_000_000
        assert row.delta_usd == 20_000_000
        assert row.delta_pct == pytest.approx(200.0)

    def test_a_sector_that_went_quiet_still_appears(self):
        """A sector with money last week and none now is a real down-mover."""
        history = {"2026-08-01": history_week(**{"Crypto": 50_000_000})}
        rows = build_sector_rows(
            [inv("Acme", 30_000_000, sector="Fintech")],
            history, ["2026-08-01"], 30_000_000,
        )
        crypto = next(r for r in rows if r.sector == "Crypto")
        assert crypto.deals == 0
        assert crypto.total_usd == 0
        assert crypto.delta_usd == -50_000_000

    def test_quiet_sector_with_no_history_is_not_invented(self):
        rows = build_sector_rows([inv("Acme", 30_000_000, sector="Fintech")], {}, [], 30_000_000)
        assert [r.sector for r in rows] == ["Fintech"]


# ---------------------------------------------------------------------------
# Stage mix
# ---------------------------------------------------------------------------

class TestStageBuckets:
    @pytest.mark.parametrize("stage,expected", [
        ("Pre-Seed", "Pre-Seed & Seed"),
        ("Seed", "Pre-Seed & Seed"),
        ("Series A", "Series A"),
        ("Series B", "Series B"),
        ("Series C", "Series C+ & Growth"),
        ("Series J", "Series C+ & Growth"),
        ("Growth", "Series C+ & Growth"),
        ("Bridge", "Other"),
        ("Unknown", "Other"),
        ("nonsense", "Other"),
        ("", "Other"),
    ])
    def test_bucketing(self, stage, expected):
        assert bucket_stage(stage) == expected

    def test_rows_are_in_stage_order_not_size_order(self):
        investments = [
            inv("Late", 900_000_000, stage="Series D"),
            inv("Early", 1_000_000, stage="Seed"),
            inv("A", 10_000_000, stage="Series A"),
        ]
        rows = build_stage_rows(investments, 911_000_000)
        assert [r.stage for r in rows] == ["Pre-Seed & Seed", "Series A", "Series C+ & Growth"]

    def test_empty_buckets_are_omitted(self):
        rows = build_stage_rows([inv("Acme", 30_000_000, stage="Series B")], 30_000_000)
        assert [r.stage for r in rows] == ["Series B"]

    def test_stage_totals_reconcile_with_the_headline(self):
        investments = [
            inv("Acme", 30_000_000, stage="Seed"),
            inv("Beta", 20_000_000, stage="Series A"),
            inv("Gamma", None, stage="Series A"),
        ]
        headline = build_headline(investments)
        rows = build_stage_rows(investments, headline.total_usd)
        assert sum(r.total_usd for r in rows) == headline.total_usd
        assert sum(r.deals for r in rows) == headline.deal_count

    def test_every_bucket_label_is_unique(self):
        labels = [label for label, _ in STAGE_BUCKETS]
        assert len(labels) == len(set(labels))


# ---------------------------------------------------------------------------
# Most active firm
# ---------------------------------------------------------------------------

class TestMostActiveFirm:
    def test_by_deal_count(self):
        result = most_active_firm([
            inv("A", 1_000_000, firms=["Antler"]),
            inv("B", 1_000_000, firms=["Antler"]),
            inv("C", 900_000_000, firms=["Sequoia"]),
        ])
        assert result.firm == "Antler"
        assert result.deals == 2

    def test_syndicated_round_counts_for_every_firm(self):
        result = most_active_firm([
            inv("A", 30_000_000, firms=["Sequoia", "Index Ventures"]),
            inv("B", 10_000_000, firms=["Sequoia"]),
        ])
        assert result.firm == "Sequoia"
        assert result.deals == 2

    def test_ties_break_on_capital(self):
        result = most_active_firm([
            inv("A", 1_000_000, firms=["Accel"]),
            inv("B", 90_000_000, firms=["Greylock"]),
        ])
        assert result.firm == "Greylock"

    def test_empty_week(self):
        assert most_active_firm([]) is None


# ---------------------------------------------------------------------------
# Movers
# ---------------------------------------------------------------------------

class TestMovers:
    def _rows(self, history, current):
        investments = [
            inv(f"C{i}", usd, sector=sector)
            for i, (sector, usd) in enumerate(current.items())
        ]
        weeks = sorted(history)
        total = sum(current.values())
        return build_sector_rows(investments, history, weeks, total)

    def test_ranked_by_dollars_not_percentage(self):
        """A $2M blip at +900% must not outrank a $200M shift at +40%."""
        history = {"2026-08-01": history_week(**{"Consumer": 200_000, "Fintech": 500_000_000})}
        rows = self._rows(history, {"Consumer": 2_000_000, "Fintech": 700_000_000})
        up, _ = build_movers(rows)
        assert up[0].sector == "Fintech"

    def test_direction_is_labelled(self):
        history = {"2026-08-01": history_week(**{"Crypto": 50_000_000})}
        rows = self._rows(history, {"Fintech": 30_000_000})
        up, down = build_movers(rows)
        assert [m.sector for m in up] == ["Fintech"]
        assert [m.sector for m in down] == ["Crypto"]
        assert down[0].delta_usd == -50_000_000

    def test_unchanged_sectors_are_not_movers(self):
        history = {"2026-08-01": history_week(**{"Fintech": 30_000_000})}
        rows = self._rows(history, {"Fintech": 30_000_000})
        up, down = build_movers(rows)
        assert up == [] and down == []

    def test_no_baseline_means_no_movers(self):
        rows = self._rows({}, {"Fintech": 30_000_000})
        up, down = build_movers(rows)
        assert up == [] and down == []

    def test_limit_is_respected(self):
        history = {"2026-08-01": history_week(**{s: 1_000_000 for s in
                   ("Fintech", "Consumer", "Security", "Crypto")})}
        rows = self._rows(history, {
            "Fintech": 90_000_000, "Consumer": 80_000_000,
            "Security": 70_000_000, "Crypto": 60_000_000,
        })
        up, _ = build_movers(rows, limit=3)
        assert len(up) == 3


# ---------------------------------------------------------------------------
# The whole digest
# ---------------------------------------------------------------------------

class TestBuildDigest:
    def test_assembles_everything(self):
        history = {"2026-08-01": history_week(**{"AI Infrastructure": 10_000_000})}
        digest = build_digest(
            [inv("Acme", 30_000_000), inv("Beta", None, sector="Fintech")],
            history, WEEK,
            source_summaries=[SourceSummary(source="Greylock", posts_found=5)],
        )
        assert digest.subject == "Trend Tracker August 2-8 2026"
        assert digest.week_label == "August 2-8 2026"
        assert digest.headline.total_usd == 30_000_000
        assert digest.headline.deal_count == 2
        assert digest.baseline_weeks == 1
        assert digest.has_baseline is True
        assert digest.most_active_firm.firm == "Greylock"
        assert digest.is_empty is False

    def test_empty_week_does_not_raise(self):
        """PLAN §7: silence must always mean breakage, so an empty week still
        has to produce a renderable digest."""
        digest = build_digest([], {}, WEEK, source_summaries=[
            SourceSummary(source="Contrary", posts_found=0),
        ])
        assert digest.is_empty is True
        assert digest.headline.deal_count == 0
        assert digest.sectors == []
        assert digest.stages == []
        assert digest.most_active_firm is None
        assert digest.subject == "Trend Tracker August 2-8 2026"

    def test_cold_start_reports_no_baseline(self):
        digest = build_digest([inv("Acme", 30_000_000)], {}, WEEK)
        assert digest.baseline_weeks == 0
        assert digest.has_baseline is False
        assert all(r.delta_usd is None for r in digest.sectors)

    def test_saving_the_week_first_does_not_pollute_its_own_baseline(self):
        """Order-independence: state.save_week() keys on the same date."""
        saved = {WEEK.isoformat(): history_week(**{"AI Infrastructure": 30_000_000})}
        digest = build_digest([inv("Acme", 30_000_000)], saved, WEEK)
        assert digest.baseline_weeks == 0
        assert digest.sectors[0].delta_usd is None

    def test_unhealthy_sources_are_surfaced(self):
        digest = build_digest([], {}, WEEK, source_summaries=[
            SourceSummary(source="Greylock", posts_found=12),
            SourceSummary(source="Battery Ventures", posts_found=0),
            SourceSummary(source="NEA", error="timeout"),
        ])
        assert {s.source for s in digest.unhealthy_sources} == {"Battery Ventures", "NEA"}

    def test_baseline_window_is_four_weeks(self):
        history = {
            week: history_week(**{"Fintech": 10_000_000})
            for week in (
                "2026-06-27", "2026-07-04", "2026-07-11",
                "2026-07-18", "2026-07-25", "2026-08-01",
            )
        }
        digest = build_digest([inv("A", 10_000_000, sector="Fintech")], history, WEEK)
        assert digest.baseline_weeks == BASELINE_WEEKS
        # The two oldest weeks fall outside the window.
        assert digest.sectors[0].avg_4w_usd == 10_000_000
