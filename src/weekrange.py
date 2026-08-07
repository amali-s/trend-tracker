"""The week window and the subject line.

The digest covers a **Sunday–Saturday** week. That isn't an arbitrary choice —
it's read off the required subject line. `Trend Tracker August 2-8 2026` spans
August 2, 2026 (a Sunday) through August 8 (a Saturday), so the window opens on
Sunday and closes on Saturday, and every function here is keyed off the closing
Saturday.

A week is identified everywhere in this codebase by its **end** date, because
that's the value the subject line, `state.save_week()` and `weekly_history.json`
all key on. `week_window()` derives the start; nothing else should.

The cron fires Sunday morning and reports on the week that closed the night
before, which is what `latest_closed_week()` returns: the most recent Saturday
*strictly* before the run date. Run mid-week by hand, it still returns a closed
week rather than a partial one — a Friday run reports the previous Sunday
through Saturday, not the three-and-a-bit days since Sunday. Use `--week` to
report on a specific week.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# date.weekday() is Monday=0 .. Sunday=6.
SATURDAY = 5
DAYS_IN_WEEK = 7


def _as_date(value: date | datetime) -> date:
    """Accept either a date or a datetime; models.py deals in datetimes."""
    return value.date() if isinstance(value, datetime) else value


def week_window(end: date | datetime) -> tuple[date, date]:
    """The (Sunday, Saturday) pair for the week ending on `end`.

    Inclusive at both ends — the span is seven days, not six.
    """
    end = _as_date(end)
    return end - timedelta(days=DAYS_IN_WEEK - 1), end


def week_label(end: date | datetime) -> str:
    """Human-readable week range, in the form the subject line requires.

        August 2-8 2026                      same month
        August 30 - September 5 2026         crosses a month
        December 27 2025 - January 2 2026    crosses a year

    Days are unpadded ("August 2-8", not "August 02-08"), which is why this
    builds the day numbers with `.day` rather than a `%d` format code.
    """
    start, end = week_window(end)
    if start.month == end.month:
        return f"{start:%B} {start.day}-{end.day} {end.year}"
    if start.year == end.year:
        return f"{start:%B} {start.day} - {end:%B} {end.day} {end.year}"
    return f"{start:%B} {start.day} {start.year} - {end:%B} {end.day} {end.year}"


def subject_line(end: date | datetime) -> str:
    """The email subject.

    No emoji. vc-job-agent's emailer opens its subject with one; the spec here
    is an exact string and doesn't have it.
    """
    return f"Trend Tracker {week_label(end)}"


def latest_closed_week(today: date | datetime | None = None) -> date:
    """The Saturday ending the most recent *fully closed* week.

    Strictly before `today`, so a Saturday run reports the week before rather
    than the one it's standing in the last day of.
    """
    today = _as_date(today) if today is not None else datetime.utcnow().date()
    # 0 would mean "today is Saturday"; roll back a full week to stay strict.
    offset = (today.weekday() - SATURDAY) % DAYS_IN_WEEK or DAYS_IN_WEEK
    return today - timedelta(days=offset)


def parse_week_arg(value: str) -> date:
    """Parse and validate the `--week YYYY-MM-DD` argument.

    The date must be a Saturday. A non-Saturday would silently produce a
    window running Wednesday–Tuesday (or whatever), which would then be stored
    in `weekly_history.json` under a key that doesn't line up with any other
    week — quietly corrupting the four-week comparison rather than failing.
    So a typo is an error, not a shrug.

    Raises ValueError; main.py catches it and reports it as a CLI error.
    """
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(
            f"--week expects a date as YYYY-MM-DD, got {value!r}"
        ) from None

    if parsed.weekday() != SATURDAY:
        # Suggest the Saturday that closes the week the given date falls in,
        # which is almost always what was meant.
        suggestion = parsed + timedelta(
            days=(SATURDAY - parsed.weekday()) % DAYS_IN_WEEK
        )
        raise ValueError(
            f"--week must be a Saturday (the day a week closes), but "
            f"{parsed.isoformat()} is a {parsed:%A}. "
            f"Did you mean {suggestion.isoformat()}?"
        )

    return parsed
