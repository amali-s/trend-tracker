"""Tests for the Sunday–Saturday week window and the subject line.

Pure date arithmetic, so these are exhaustive where it's cheap to be: every
weekday is checked for `latest_closed_week`, and every branch of `week_label`
has a case built from a real calendar week rather than a hand-written string.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.weekrange import (  # noqa: E402
    SATURDAY,
    latest_closed_week,
    parse_week_arg,
    subject_line,
    week_label,
    week_window,
)

# The week named in the spec: August 2, 2026 is a Sunday, August 8 a Saturday.
SPEC_WEEK_END = date(2026, 8, 8)


class TestWeekWindow:
    def test_spans_seven_days_inclusive(self):
        start, end = week_window(SPEC_WEEK_END)
        assert start == date(2026, 8, 2)
        assert end == SPEC_WEEK_END
        assert (end - start).days == 6

    def test_starts_sunday_ends_saturday(self):
        start, end = week_window(SPEC_WEEK_END)
        assert start.strftime("%A") == "Sunday"
        assert end.strftime("%A") == "Saturday"

    def test_accepts_a_datetime(self):
        """models.py deals in datetimes; the window must not choke on one."""
        start, end = week_window(datetime(2026, 8, 8, 13, 30))
        assert (start, end) == (date(2026, 8, 2), date(2026, 8, 8))


class TestWeekLabel:
    def test_same_month(self):
        assert week_label(SPEC_WEEK_END) == "August 2-8 2026"

    def test_crosses_a_month(self):
        # Aug 30 2026 is a Sunday; the week closes Sep 5.
        assert week_label(date(2026, 9, 5)) == "August 30 - September 5 2026"

    def test_crosses_a_year(self):
        # Dec 27 2025 is a Sunday; the week closes Jan 2 2026.
        assert week_label(date(2026, 1, 2)) == "December 27 2025 - January 2 2026"

    def test_days_are_not_zero_padded(self):
        """"August 2-8", not "August 02-08" — %d would pad and break the spec.

        Checked against the month name rather than the bare digits, because
        the year itself contains "02".
        """
        assert "August 02" not in week_label(SPEC_WEEK_END)
        assert week_label(SPEC_WEEK_END).endswith("2-8 2026")
        assert "September 05" not in week_label(date(2026, 9, 5))

    def test_every_branch_names_both_endpoints(self):
        """Whatever the branch, the label must mention both days of the window."""
        end = date(2026, 1, 3)
        for _ in range(60):  # a full year of weeks, across both boundaries
            start, _end = week_window(end)
            label = week_label(end)
            assert str(start.day) in label
            assert str(end.day) in label
            assert start.strftime("%B") in label
            assert end.strftime("%B") in label
            end += timedelta(days=7)


class TestSubjectLine:
    def test_matches_the_spec_exactly(self):
        assert subject_line(SPEC_WEEK_END) == "Trend Tracker August 2-8 2026"

    def test_carries_no_emoji(self):
        """vc-job-agent opens its subject with an emoji; this spec doesn't."""
        subject = subject_line(SPEC_WEEK_END)
        assert subject.isascii()
        assert subject.startswith("Trend Tracker ")


class TestLatestClosedWeek:
    def test_sunday_run_reports_the_week_that_just_closed(self):
        """The cron case: Sunday Aug 9 reports Aug 2-8."""
        assert latest_closed_week(date(2026, 8, 9)) == date(2026, 8, 8)
        assert week_label(latest_closed_week(date(2026, 8, 9))) == "August 2-8 2026"

    def test_saturday_run_does_not_report_the_day_it_is_standing_in(self):
        """Strictly-before: a Saturday run reports the *previous* week."""
        assert latest_closed_week(date(2026, 8, 8)) == date(2026, 8, 1)

    def test_midweek_run_reports_a_closed_week_not_a_partial_one(self):
        assert latest_closed_week(date(2026, 8, 7)) == date(2026, 8, 1)

    @pytest.mark.parametrize("offset", range(7))
    def test_always_returns_a_saturday_strictly_in_the_past(self, offset):
        today = date(2026, 8, 2) + timedelta(days=offset)  # Sunday .. Saturday
        result = latest_closed_week(today)
        assert result.weekday() == SATURDAY
        assert result < today
        assert (today - result).days <= 7

    def test_accepts_a_datetime(self):
        assert latest_closed_week(datetime(2026, 8, 9, 13, 0)) == date(2026, 8, 8)

    def test_defaults_to_today(self):
        result = latest_closed_week()
        assert result.weekday() == SATURDAY
        assert result < datetime.utcnow().date()


class TestParseWeekArg:
    def test_accepts_a_saturday(self):
        assert parse_week_arg("2026-08-08") == SPEC_WEEK_END

    def test_tolerates_surrounding_whitespace(self):
        assert parse_week_arg("  2026-08-08 ") == SPEC_WEEK_END

    def test_rejects_a_non_saturday(self):
        """A Wednesday would key weekly_history.json off a misaligned window."""
        with pytest.raises(ValueError, match="must be a Saturday"):
            parse_week_arg("2026-08-05")

    def test_suggests_the_saturday_closing_that_week(self):
        with pytest.raises(ValueError, match="2026-08-08"):
            parse_week_arg("2026-08-05")

    def test_rejects_a_malformed_date(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            parse_week_arg("last tuesday")

    def test_rejects_an_impossible_date(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            parse_week_arg("2026-02-30")
