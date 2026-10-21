"""The cost questions, composed once for every front end (the CLI and the MCP server).

Each function takes an already-built Cost Explorer client (live boto3 client or FixtureCostExplorer) and
returns the existing report models. Billing arithmetic stays in bill.py and compare.py, anomaly logic in
anomalies.py and spoken wording in voice.py. Nothing here selects an AWS account, profile or role: the
caller decides that once, when it builds the client.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum

from cloudguard.cost.anomalies import MAX_LOOKBACK_DAYS, get_cost_anomalies
from cloudguard.cost.bill import get_bill
from cloudguard.cost.compare import get_comparison
from cloudguard.cost.models import (
    AnomalyReport, AnomalyStatus, BillReport, BillStatus, CostComparison, Period, PeriodKind, RankedService,
    ServiceRanking,
)
from cloudguard.cost.periods import month_before, month_start
from cloudguard.cost.voice import bill_speech, comparison_speech, increase_speech, month_to_date_speech, top_services

_MONTHS = {name: i for i, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
RANKING_BASIS = "charges before discounts, credits and refunds (UnblendedCost); tax-only lines excluded"


class Outcome(StrEnum):
    """What a front end should do with an answer (the CLI maps these to exit codes 0, 1 and 3)."""

    ANSWERED = "answered"  # includes "no anomalies" and "the cause can't be determined"
    FINDINGS = "findings"  # AWS Cost Anomaly Detection reported anomalies
    UNAVAILABLE = "unavailable"  # AWS/API error or data unavailable: no figures are given


class MonthError(ValueError):
    """A month argument that is malformed or in the future."""


def parse_month(text: str, as_of: date) -> date:
    """First day of the month named by `text`, relative to `as_of`. Never after the as-of month.

    Accepts YYYY-MM, an English month name or a 3+ letter prefix (its latest occurrence not after
    `as_of`), "last-month" or "this-month".
    """
    value = text.strip().lower()
    current = month_start(as_of)
    if value == "this-month":
        return current
    if value == "last-month":
        return month_before(current)[0]
    if m := re.fullmatch(r"(\d{4})-(\d{2})", value):
        year, month = int(m[1]), int(m[2])
        if not 1 <= month <= 12 or year < 2000:
            raise MonthError(f"invalid month: {text}")
        start = date(year, month, 1)
    else:
        number = _MONTHS.get(value) or next((n for name, n in _MONTHS.items()
                                             if len(value) >= 3 and name.startswith(value)), None)
        if number is None:
            raise MonthError(f"invalid month: {text!r} (use YYYY-MM, a month name, last-month or this-month)")
        start = date(as_of.year if number <= as_of.month else as_of.year - 1, number, 1)
    if start > current:
        raise MonthError(f"{start:%B %Y} is in the future relative to {as_of.isoformat()}")
    return start


def comparison_months(earlier: str | None, later: str | None, as_of: date) -> tuple[date, date]:
    """Both months given, or neither (the last two completed months). Earlier must come first."""
    if (earlier is None) != (later is None):
        raise MonthError("give both months, or neither for the last two completed months")
    if earlier is None:
        later_start = month_before(month_start(as_of))[0]
        earlier_start = month_before(later_start)[0]
    else:
        earlier_start, later_start = parse_month(earlier, as_of), parse_month(later, as_of)
    if earlier_start >= later_start:
        raise MonthError("the earlier month must come before the later month")
    return earlier_start, later_start


def bill_outcome(status: BillStatus) -> Outcome:
    return Outcome.ANSWERED if status == BillStatus.OK else Outcome.UNAVAILABLE


def anomaly_outcome(status: AnomalyStatus) -> Outcome:
    return {AnomalyStatus.ANOMALIES_FOUND: Outcome.FINDINGS,
            AnomalyStatus.NO_ANOMALIES: Outcome.ANSWERED}.get(status, Outcome.UNAVAILABLE)


def month_costs(client, period: Period, now: datetime, max_pages: int | None = None) -> BillReport:
    """A completed month (compared with the month before) or the current month to date (no comparison)."""
    compare_start = month_before(period.start)[0] if period.kind == PeriodKind.COMPLETED_MONTH else None
    return get_bill(client, period.start, period.end_exclusive, now, compare_start=compare_start,
                    max_pages=max_pages)


def month_speech(report: BillReport, period: Period) -> str:
    return month_to_date_speech(report, period) if period.kind == PeriodKind.MONTH_TO_DATE else bill_speech(report)


def ranking(report: BillReport, limit: int | None = None) -> ServiceRanking:
    """Services by charges, tax excluded from the ranking but disclosed beside it."""
    if report.month is None:
        return ServiceRanking(basis=RANKING_BASIS, services=[], excluded_tax=None)
    ranked = top_services(report.month, limit=len(report.month.services))
    tax = sum((s.amount for s in report.month.services if s.tax_only), Decimal(0))
    shown = ranked if limit is None else ranked[:limit]
    return ServiceRanking(basis=RANKING_BASIS, excluded_tax=tax, total_services=len(ranked),
                          truncated=len(shown) < len(ranked),
                          services=[RankedService(rank=i, service=s.service, amount=s.amount)
                                    for i, s in enumerate(shown, 1)])


def compare(client, earlier: date, later: date, as_of: date, now: datetime,
            max_pages: int | None = None) -> CostComparison:
    return get_comparison(client, earlier, later, as_of, now, max_pages=max_pages)


def compare_speech(report: CostComparison, *, explain: bool) -> str:
    return increase_speech(report) if explain else comparison_speech(report)


def anomalies(client, as_of: date, lookback_days: int, now: datetime, max_pages: int | None = None) -> AnomalyReport:
    """Anomalies for the window ending at `as_of`; generated_at stays the real time of this run."""
    if not 1 <= lookback_days <= MAX_LOOKBACK_DAYS:
        raise ValueError(f"lookback_days must be between 1 and {MAX_LOOKBACK_DAYS}")
    pinned = datetime.combine(as_of, time(12, 0), UTC)
    report = get_cost_anomalies(client, pinned, lookback_days, max_pages=max_pages)
    return report.model_copy(update={"generated_at": now})
