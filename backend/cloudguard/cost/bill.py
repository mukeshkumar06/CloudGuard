"""Previous AWS billing month (UTC) from Cost Explorer, UnblendedCost only.

One GetCostAndUsage request covers last month and the month before it, grouped by SERVICE and
RECORD_TYPE. Each line is classified by RECORD_TYPE into exactly one category (record_types.py);
unknown record types stay unclassified and are never counted as charges.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from botocore.exceptions import BotoCoreError, ClientError

from cloudguard.cost.ce import DATA_UNAVAILABLE, PageLimitExceeded, RequestLog, paginate, reduce_error, resolve_max_pages
from cloudguard.cost.models import (
    METRIC, BillReport, BillStatus, MonthComparison, MonthCost, RecordTypeLine, ReportWarning, ServiceCost,
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


def _compare(last: MonthCost, prev_start: date, prev: _Month | None) -> MonthComparison | None:
    if prev is None or prev.lines == 0:
        return None
    current = last.charges_before_discounts_credits_and_refunds
    change = current - prev.charges
    percent = None if prev.charges == 0 else (change / prev.charges * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)
    return MonthComparison(previous_start=prev_start, previous_charges=prev.charges, change=change,
                           change_percent=percent, estimated=last.estimated or prev.estimated)


def get_last_month_bill(client, now: datetime | None = None, *, max_pages: int | None = None,
                        clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> BillReport:
    now = now or clock()
    pages = resolve_max_pages(max_pages)
    start, end = previous_billing_month(now)
    prev_start, _ = month_before(start)
    log = RequestLog(clock=clock)

    def report(**fields) -> BillReport:
        return BillReport(period_start=start, period_end_exclusive=end, generated_at=now,
                          retrieved_at=log.last_success_at, aws_requests=log.count, **fields)

    try:
        results = paginate(
            client.get_cost_and_usage, "ResultsByTime", log, pages,
            TimePeriod={"Start": prev_start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=[METRIC],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
        )
    except (ClientError, BotoCoreError, PageLimitExceeded) as exc:
        error, denied = reduce_error(exc, OPERATION)
        status = (BillStatus.DATA_UNAVAILABLE if error.code == DATA_UNAVAILABLE
                  else BillStatus.ACCESS_DENIED if denied else BillStatus.ERROR)
        return report(status=status, error=error)

    months, units = _aggregate(results)
    if len(units) > 1:
        return report(status=BillStatus.MIXED_CURRENCY)
    last = months.get(start)
    if last is None or last.lines == 0:
        return report(status=BillStatus.NO_DATA, currency=next(iter(units), None))
    month = last.to_model(start)
    comparison = _compare(month, prev_start, months.get(prev_start))
    warnings = []
    if month.estimated or (comparison is not None and comparison.estimated):
        warnings.append(ReportWarning.ESTIMATED)
    if month.unclassified_record_types:
        warnings.append(ReportWarning.UNCLASSIFIED_RECORD_TYPES)
    return report(status=BillStatus.OK, currency=units.pop(), month=month, comparison=comparison, warnings=warnings)
