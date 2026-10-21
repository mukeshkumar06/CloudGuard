"""AWS billing months. Canonical basis for Task 1: UTC calendar months, as AWS bills them.

Cost Explorer days and months are UTC. Dates sent to it are date-only strings; End is exclusive.
No local timezone shifts these boundaries (local-calendar reporting is a separate, later feature).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

BILLING_TIMEZONE = "UTC"


def month_start(d: date) -> date:
    return d.replace(day=1)


def utc_date(now: datetime) -> date:
    """The UTC calendar date of an aware datetime. Naive datetimes are ambiguous and refused."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC).date()


def previous_billing_month(now: datetime) -> tuple[date, date]:
    """(start, end_exclusive) of the AWS billing month before the one `now` falls in, in UTC."""
    end = month_start(utc_date(now))
    return month_start(end - timedelta(days=1)), end


def month_before(start: date) -> tuple[date, date]:
    """The calendar month immediately before the month that begins at `start`."""
    return month_start(start - timedelta(days=1)), start


def next_month(start: date) -> date:
    """The first day of the month after the month containing `start`."""
    return (month_start(start) + timedelta(days=32)).replace(day=1)


def days_in_month(d: date) -> int:
    return (next_month(d) - month_start(d)).days


def month_to_date_end(as_of: date) -> date:
    """Exclusive end of the current month to date: the day after `as_of`, so `as_of` itself is included.

    Cost Explorer data for the latest day is usually incomplete (AWS refreshes it at least daily).
    NOT YET VERIFIED that Cost Explorer accepts an End of tomorrow for GetCostAndUsage.
    """
    return as_of + timedelta(days=1)
