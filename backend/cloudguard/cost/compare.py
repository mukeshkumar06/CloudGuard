"""Compare two billing months and locate where a cost increase appears.

Reuses bill.fetch_month_costs: one GetCostAndUsage request for both months, plus one more when the later
month is still in progress (the earlier month is then cut to the same number of days, so a partial month
is never compared with a full one).

Attribution is arithmetic only. The calculated net change splits exactly into per-service charge changes
plus discount, credit, refund and unclassified changes. Those contributions say where the change
appears, never why it happened.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from botocore.exceptions import BotoCoreError, ClientError

from cloudguard.cost.bill import failure, fetch_month_costs, has_data
from cloudguard.cost.ce import PageLimitExceeded, RequestLog, resolve_max_pages
from cloudguard.cost.models import (
    MATERIALITY_THRESHOLD, BillStatus, CategoryChange, ComparisonBasis, CostComparison, Driver, DriverKind,
    IncreaseExplanation, IncreaseVerdict, MonthCost, Period, PeriodKind, ReportWarning, ServiceChange,
)
from cloudguard.cost.periods import days_in_month, month_start, month_to_date_end, next_month
from cloudguard.cost.record_types import Category

_PCT = Decimal("0.1")

# MonthCost field for each category, in reporting order.
CATEGORY_FIELDS = [
    (Category.USAGE, "usage"), (Category.SAVINGS_PLAN_NEGATION, "savings_plan_negation"), (Category.FEE, "fees"),
    (Category.TAX, "tax"), (Category.DISCOUNT, "discounts"), (Category.CREDIT, "credits"),
    (Category.REFUND, "refunds"), (Category.UNCLASSIFIED, "unclassified"),
]
_ADJUSTMENTS = ("discounts", "credits", "refunds")


def month_period(start: date, as_of: date) -> Period:
    """The month starting at `start`: completed, or month to date if `as_of` falls in it."""
    start = month_start(start)
    if start > month_start(as_of):
        raise ValueError(f"{start:%B %Y} is after the as-of date {as_of.isoformat()}")
    current = start == month_start(as_of)
    end = month_to_date_end(as_of) if current else next_month(start)
    return Period(kind=PeriodKind.MONTH_TO_DATE if current else PeriodKind.COMPLETED_MONTH,
                  label=f"{start:%B %Y}", start=start, end_exclusive=end, days=(end - start).days,
                  days_in_month=days_in_month(start))


def _percent(change: Decimal, base: Decimal) -> Decimal | None:
    return None if base == 0 else (change / base * 100).quantize(_PCT, ROUND_HALF_UP)


def service_changes(earlier: MonthCost, later: MonthCost) -> list[ServiceChange]:
    before = {s.service: s for s in earlier.services}
    after = {s.service: s for s in later.services}
    rows = []
    for name in before.keys() | after.keys():
        b, a = before.get(name), after.get(name)
        old, new = (b.amount if b else Decimal(0)), (a.amount if a else Decimal(0))
        rows.append(ServiceChange(service=name, earlier=old, later=new, change=new - old,
                                  tax_only=all(x.tax_only for x in (b, a) if x)))
    return sorted(rows, key=lambda r: (-r.change, r.service))


def category_changes(earlier: MonthCost, later: MonthCost) -> list[CategoryChange]:
    return [CategoryChange(category=c, earlier=getattr(earlier, f), later=getattr(later, f),
                           change=getattr(later, f) - getattr(earlier, f)) for c, f in CATEGORY_FIELDS]


def explain_change(earlier: MonthCost, later: MonthCost, services: list[ServiceChange]) -> IncreaseExplanation:
    """Split the calculated net change into contributions. States locations, never causes.

    Contributions sum exactly to the net change. Only those at least MATERIALITY_THRESHOLD in magnitude are
    listed as drivers or offsets; the rest are totalled in below_threshold_change. A net change smaller than
    the threshold is NO_MATERIAL_CHANGE, so Decimal residues of nearly cancelling lines are never an increase.
    """
    parts = [Driver(kind=DriverKind.SERVICE, name=s.service, change=s.change) for s in services if s.change]
    for name in _ADJUSTMENTS:
        if delta := getattr(later, name) - getattr(earlier, name):
            parts.append(Driver(kind=DriverKind.ADJUSTMENT, name=name, change=delta))
    if delta := later.unclassified - earlier.unclassified:
        parts.append(Driver(kind=DriverKind.UNCLASSIFIED, name="unclassified", change=delta))

    net = later.calculated_net - earlier.calculated_net
    gross = sum((p.change for p in parts if p.change > 0), Decimal(0))
    material = [p for p in parts if abs(p.change) >= MATERIALITY_THRESHOLD]
    drivers = sorted((p.model_copy(update={"share_of_gross_increase_percent": _percent(p.change, gross)})
                      for p in material if p.change > 0), key=lambda p: (-p.change, p.name))
    offsets = sorted((p for p in material if p.change < 0), key=lambda p: (p.change, p.name))
    below = sum((p.change for p in parts if abs(p.change) < MATERIALITY_THRESHOLD), Decimal(0))

    reasons = []
    if abs(net) < MATERIALITY_THRESHOLD:
        verdict = IncreaseVerdict.NO_MATERIAL_CHANGE
    elif net < 0:
        verdict = IncreaseVerdict.NO_INCREASE
    else:
        if not drivers:
            reasons.append(f"the increase is spread across changes that are each smaller than "
                           f"{MATERIALITY_THRESHOLD}, so no single service or charge type accounts for it")
        elif drivers[0].kind == DriverKind.UNCLASSIFIED:
            reasons.append("the largest contribution is from billing record types CloudGuard cannot classify, "
                           "so what they are can't be determined")
        verdict = IncreaseVerdict.CANNOT_DETERMINE if reasons else IncreaseVerdict.INCREASE_ATTRIBUTED
    return IncreaseExplanation(verdict=verdict, net_change=net, gross_increase=gross, drivers=drivers,
                               offsets=offsets, reasons=reasons, below_threshold_change=below)


def get_comparison(client, earlier_start: date, later_start: date, as_of: date, now: datetime | None = None, *,
                   max_pages: int | None = None,
                   clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> CostComparison:
    later = month_period(later_start, as_of)
    earlier = month_period(earlier_start, as_of)
    if earlier.start >= later.start:
        raise ValueError("the earlier month must be before the later month")
    now = now or clock()
    pages = resolve_max_pages(max_pages)
    log = RequestLog(clock=clock)

    full_earlier = None
    basis = ComparisonBasis.FULL_MONTHS
    if later.kind == PeriodKind.MONTH_TO_DATE:
        basis = ComparisonBasis.MATCHING_DAYS
        cut = min(earlier.start + timedelta(days=later.days), earlier.end_exclusive)
        if cut < earlier.end_exclusive:
            full_earlier = earlier
            earlier = earlier.model_copy(update={"kind": PeriodKind.PARTIAL_MONTH, "end_exclusive": cut,
                                                 "days": (cut - earlier.start).days})

    def report(**fields) -> CostComparison:
        return CostComparison(generated_at=now, retrieved_at=log.last_success_at, aws_requests=log.count,
                              basis=basis, earlier=earlier, later=later, earlier_full_month=full_earlier, **fields)

    try:
        months, units = fetch_month_costs(client, earlier.start, later.end_exclusive, log, pages)
        earlier_cost, full_cost = months.get(earlier.start), None
        if full_earlier is not None:
            full_cost = earlier_cost
            cut_months, cut_units = fetch_month_costs(client, earlier.start, earlier.end_exclusive, log, pages)
            earlier_cost, units = cut_months.get(earlier.start), units | cut_units
    except (ClientError, BotoCoreError, PageLimitExceeded) as exc:
        status, error = failure(exc)
        return report(status=status, error=error)

    if len(units) > 1:
        return report(status=BillStatus.MIXED_CURRENCY)
    later_cost = months.get(later.start)
    currency = next(iter(units), None)
    seen = dict(earlier_month=earlier_cost if has_data(earlier_cost) else None,
                later_month=later_cost if has_data(later_cost) else None,
                earlier_full_month_cost=full_cost if has_data(full_cost) else None)
    if not (has_data(earlier_cost) and has_data(later_cost)):
        return report(status=BillStatus.NO_DATA, currency=currency, **seen)

    services = service_changes(earlier_cost, later_cost)
    charges_old = earlier_cost.charges_before_discounts_credits_and_refunds
    charges_new = later_cost.charges_before_discounts_credits_and_refunds
    warnings = []
    if earlier_cost.estimated or later_cost.estimated:
        warnings.append(ReportWarning.ESTIMATED)
    if earlier_cost.unclassified_record_types or later_cost.unclassified_record_types:
        warnings.append(ReportWarning.UNCLASSIFIED_RECORD_TYPES)
    if later.kind == PeriodKind.MONTH_TO_DATE:
        warnings.append(ReportWarning.MONTH_TO_DATE)
    return report(status=BillStatus.OK, currency=currency, **seen,
                  charges_change=charges_new - charges_old,
                  charges_change_percent=_percent(charges_new - charges_old, charges_old),
                  net_change=later_cost.calculated_net - earlier_cost.calculated_net,
                  service_changes=services, category_changes=category_changes(earlier_cost, later_cost),
                  explanation=explain_change(earlier_cost, later_cost, services), warnings=warnings)
