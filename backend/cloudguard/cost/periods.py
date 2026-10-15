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
