"""AWS billing-month costs (UTC) from Cost Explorer, UnblendedCost only.

One GetCostAndUsage request covers the month (or month to date) and, optionally, an earlier month to
compare with, grouped by SERVICE and RECORD_TYPE. Each line is classified by RECORD_TYPE into exactly
one category (record_types.py); unknown record types stay unclassified and are never counted as charges.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from botocore.exceptions import BotoCoreError, ClientError

from cloudguard.cost.ce import DATA_UNAVAILABLE, PageLimitExceeded, RequestLog, paginate, reduce_error, resolve_max_pages
from cloudguard.cost.models import (
    METRIC, BillReport, BillStatus, CostError, MonthComparison, MonthCost, RecordTypeLine, ReportWarning, ServiceCost,
)
from cloudguard.cost.periods import month_before, previous_billing_month
from cloudguard.cost.record_types import CHARGE_CATEGORIES, Category, classify

OPERATION = "ce:GetCostAndUsage"


class _Month:
    def __init__(self) -> None:
        self.end: date | None = None
        self.estimated = False
        self.lines = 0
        self.by_category: defaultdict[Category, Decimal] = defaultdict(Decimal)
        self.by_record_type: defaultdict[str, Decimal] = defaultdict(Decimal)
        self.services: defaultdict[str, Decimal] = defaultdict(Decimal)
        self.non_tax_services: set[str] = set()

    def add(self, service: str, record_type: str, amount: Decimal) -> None:
        category, _ = classify(record_type)
        self.lines += 1
        self.by_category[category] += amount
        self.by_record_type[record_type] += amount
        if category in CHARGE_CATEGORIES:
            self.services[service] += amount
            if category != Category.TAX:
                self.non_tax_services.add(service)

    @property
    def charges(self) -> Decimal:
        return sum((self.by_category[c] for c in CHARGE_CATEGORIES), Decimal(0))

    def to_model(self, start: date) -> MonthCost:
        cat = self.by_category
        record_types = []
        for name, amount in sorted(self.by_record_type.items()):
            category, source = classify(name)
            record_types.append(RecordTypeLine(record_type=name, category=category, match_source=source, amount=amount))
        services = sorted((ServiceCost(service=s, amount=a, tax_only=s not in self.non_tax_services)
                           for s, a in self.services.items() if a != 0),
                          key=lambda sc: (-sc.amount, sc.service))
        return MonthCost(
            start=start, end_exclusive=self.end, estimated=self.estimated,
            charges_before_discounts_credits_and_refunds=self.charges,
            usage=cat[Category.USAGE], savings_plan_negation=cat[Category.SAVINGS_PLAN_NEGATION],
            fees=cat[Category.FEE], tax=cat[Category.TAX], discounts=cat[Category.DISCOUNT],
            credits=cat[Category.CREDIT], refunds=cat[Category.REFUND], unclassified=cat[Category.UNCLASSIFIED],
            calculated_net=sum(cat.values(), Decimal(0)),
            services=services, record_types=record_types,
            unclassified_record_types=[r.record_type for r in record_types if r.category == Category.UNCLASSIFIED],
        )


def _aggregate(results: list[dict]) -> tuple[dict[date, _Month], set[str]]:
    """Merge ResultsByTime entries by period start (pagination can split one month's groups)."""
    months: dict[date, _Month] = defaultdict(_Month)
    units: set[str] = set()
    for result in results:
        month = months[date.fromisoformat(result["TimePeriod"]["Start"])]
        month.end = date.fromisoformat(result["TimePeriod"]["End"])
        month.estimated = month.estimated or bool(result.get("Estimated", False))
        for group in result.get("Groups", []):
            service, record_type = group["Keys"]
            metric = group["Metrics"][METRIC]
            units.add(metric["Unit"])
            month.add(service, record_type, Decimal(metric["Amount"]))
    return months, units


def fetch_month_costs(client, start: date, end_exclusive: date, log: RequestLog,
                      max_pages: int) -> tuple[dict[date, MonthCost], set[str]]:
    """MONTHLY UnblendedCost by SERVICE and RECORD_TYPE for [start, end_exclusive), keyed by period start.

    A range that starts or ends mid-month yields a partial period for that month. Raises the AWS error
    (or PageLimitExceeded) instead of returning a partial result.
    """
    results = paginate(
        client.get_cost_and_usage, "ResultsByTime", log, max_pages,
        TimePeriod={"Start": start.isoformat(), "End": end_exclusive.isoformat()},
        Granularity="MONTHLY",
        Metrics=[METRIC],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
    )
    months, units = _aggregate(results)
    return {s: m.to_model(s) for s, m in months.items()}, units


def failure(exc: Exception) -> tuple[BillStatus, CostError]:
    """The bill status and reduced error for a failed GetCostAndUsage request."""
    error, denied = reduce_error(exc, OPERATION)
    status = (BillStatus.DATA_UNAVAILABLE if error.code == DATA_UNAVAILABLE
              else BillStatus.ACCESS_DENIED if denied else BillStatus.ERROR)
    return status, error


def has_data(month: MonthCost | None) -> bool:
    return month is not None and bool(month.record_types)


def compare_months(current: MonthCost, previous: MonthCost | None) -> MonthComparison | None:
    if not has_data(previous):
        return None
    now_charges = current.charges_before_discounts_credits_and_refunds
    prev_charges = previous.charges_before_discounts_credits_and_refunds
    change = now_charges - prev_charges
    percent = None if prev_charges == 0 else (change / prev_charges * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)
    return MonthComparison(previous_start=previous.start, previous_charges=prev_charges, change=change,
                           change_percent=percent, estimated=current.estimated or previous.estimated)


def get_bill(client, start: date, end_exclusive: date, now: datetime | None = None, *,
             compare_start: date | None = None, max_pages: int | None = None,
             clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> BillReport:
    """Costs for [start, end_exclusive) in one request, optionally compared with the period at compare_start.

    end_exclusive may fall mid-month (month to date); the report then covers a partial month and the
    caller must not present it as a final bill.
    """
    now = now or clock()
    pages = resolve_max_pages(max_pages)
    log = RequestLog(clock=clock)

    def report(**fields) -> BillReport:
        return BillReport(period_start=start, period_end_exclusive=end_exclusive, generated_at=now,
                          retrieved_at=log.last_success_at, aws_requests=log.count, **fields)

    try:
        months, units = fetch_month_costs(client, compare_start or start, end_exclusive, log, pages)
    except (ClientError, BotoCoreError, PageLimitExceeded) as exc:
        status, error = failure(exc)
        return report(status=status, error=error)

    if len(units) > 1:
        return report(status=BillStatus.MIXED_CURRENCY)
    month = months.get(start)
    if not has_data(month):
        return report(status=BillStatus.NO_DATA, currency=next(iter(units), None))
    comparison = compare_months(month, months.get(compare_start)) if compare_start else None
    warnings = []
    if month.estimated or (comparison is not None and comparison.estimated):
        warnings.append(ReportWarning.ESTIMATED)
    if month.unclassified_record_types:
        warnings.append(ReportWarning.UNCLASSIFIED_RECORD_TYPES)
    return report(status=BillStatus.OK, currency=units.pop(), month=month, comparison=comparison, warnings=warnings)


def get_last_month_bill(client, now: datetime | None = None, *, max_pages: int | None = None,
                        clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> BillReport:
    now = now or clock()
    start, end = previous_billing_month(now)
    prev_start, _ = month_before(start)
    return get_bill(client, start, end, now, compare_start=prev_start, max_pages=max_pages, clock=clock)
