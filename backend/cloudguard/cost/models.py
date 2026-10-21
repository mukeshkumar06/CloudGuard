"""Result models for cost intelligence. Amounts are Decimal (AWS returns decimal strings).

Contract for MCP clients: schema changes are additive only. Fields are never renamed or removed, and
new fields are optional. Nothing here carries an account ID, ARN or raw AWS error message.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from cloudguard.cost.record_types import Category, MatchSource

SCHEMA_VERSION = "1"
METRIC = "UnblendedCost"
NET_BASIS = "CloudGuard calculation: sum of every UnblendedCost line returned by Cost Explorer; not the AWS invoice amount"
# Materiality threshold for attributing a change, in units of the report currency (USD so far): one cent,
# the smallest amount an invoice shows. A net change, or a single contribution, smaller in magnitude than
# this is not reported as an increase, decrease or driver. Cost Explorer amounts carry up to ~10 decimal
# places, so totals of lines that nearly cancel (usage fully covered by credits) leave residues such as
# 1.5E-9 that are noise, not spend. Billing figures themselves are never rounded or suppressed.
MATERIALITY_THRESHOLD = Decimal("0.01")
ANOMALY_CURRENCY = "USD"
ANOMALY_CURRENCY_BASIS = ("documented_not_returned: the Cost Anomaly Detection API returns impact amounts without a "
                          "currency field; AWS's Impact documentation describes them as dollar values")
ANOMALY_CURRENCY_REFERENCE = "https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_Impact.html"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CacheInfo(_Frozen):
    """Reserved for a later caching layer; always None today."""

    hit: bool
    stored_at: datetime
    ttl_seconds: int


class _ReportMeta(_Frozen):
    schema_version: str = SCHEMA_VERSION
    generated_at: datetime  # when CloudGuard built this report
    retrieved_at: datetime | None = None  # when AWS last answered successfully; None if it never did
    aws_requests: int = 0  # Cost Explorer requests sent (each is billed); botocore retries not counted
    cache: CacheInfo | None = None


class CostError(_Frozen):
    """An AWS failure. AWS's raw message is dropped (it can contain ARNs)."""

    code: str  # reported by AWS
    operation: str
    aws_description: str | None = None  # AWS's documented meaning of `code`, when we know it
    inferred_possible_causes: list[str] = []  # CloudGuard's guesses, not AWS statements


class ReportWarning(StrEnum):
    ESTIMATED = "ESTIMATED"
    UNCLASSIFIED_RECORD_TYPES = "UNCLASSIFIED_RECORD_TYPES"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    STALE_EVALUATION = "STALE_EVALUATION"
    MONTH_TO_DATE = "MONTH_TO_DATE"  # a period is the current, unfinished month: not a final bill


# ---------- bill ----------

class BillStatus(StrEnum):
    OK = "OK"
    NO_DATA = "NO_DATA"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    ACCESS_DENIED = "ACCESS_DENIED"
    MIXED_CURRENCY = "MIXED_CURRENCY"
    ERROR = "ERROR"


class ServiceCost(_Frozen):
    service: str
    amount: Decimal  # charge categories only (usage, Savings Plan negation, fees, tax)
    tax_only: bool = False  # every line for this service is Tax (AWS reports tax as a "Tax" service)


class RecordTypeLine(_Frozen):
    """One RECORD_TYPE's total for the month: the evidence behind every category figure."""

    record_type: str  # exactly as AWS returned it
    category: Category
    match_source: MatchSource | None  # None when unclassified
    amount: Decimal


class MonthCost(_Frozen):
    """One AWS billing month (UTC). Every returned line is in exactly one category.

    charges_before_discounts_credits_and_refunds + discounts + credits + refunds + unclassified
    == calculated_net == sum(record_types.amount); sum(services.amount) == charges_before_...
    """

    start: date
    end_exclusive: date
    estimated: bool
    charges_before_discounts_credits_and_refunds: Decimal
    usage: Decimal
    savings_plan_negation: Decimal
    fees: Decimal
    tax: Decimal
    discounts: Decimal
    credits: Decimal
    refunds: Decimal
    unclassified: Decimal
    calculated_net: Decimal
    net_basis: str = NET_BASIS
    services: list[ServiceCost]
    record_types: list[RecordTypeLine]
    unclassified_record_types: list[str]


class MonthComparison(_Frozen):
    """Compares charges_before_discounts_credits_and_refunds between the two months."""

    previous_start: date
    previous_charges: Decimal
    change: Decimal
    change_percent: Decimal | None  # None when the previous month's charges were zero
    estimated: bool  # either month still estimated


class BillReport(_ReportMeta):
    status: BillStatus
    metric: str = METRIC
    billing_timezone: str = "UTC"
    period_basis: str = "AWS billing month (UTC calendar month); end date exclusive"
    period_start: date
    period_end_exclusive: date
    currency: str | None = None  # the Unit AWS returned
    month: MonthCost | None = None
    comparison: MonthComparison | None = None
    warnings: list[ReportWarning] = []
    error: CostError | None = None


# ---------- anomalies ----------

class AnomalyStatus(StrEnum):
    ANOMALIES_FOUND = "ANOMALIES_FOUND"
    NO_ANOMALIES = "NO_ANOMALIES"
    NOT_YET_EVALUATED = "NOT_YET_EVALUATED"
    NO_MONITOR = "NO_MONITOR"
    ACCESS_DENIED = "ACCESS_DENIED"
    ERROR = "ERROR"


class Anomaly(_Frozen):
    service: str | None
    region: str | None
    usage_type: str | None
    start_date: date | None
    end_date: date | None  # None: AWS has not closed the anomaly
    expected_spend: Decimal | None
    actual_spend: Decimal | None
    impact: Decimal
    impact_percent: Decimal | None  # AWS omits it when expected spend is zero
    score: Decimal
    explanation: str


class AnomalyReport(_ReportMeta):
    status: AnomalyStatus
    window_start: date
    window_end: date
    currency: str = ANOMALY_CURRENCY
    currency_basis: str = ANOMALY_CURRENCY_BASIS
    currency_reference: str = ANOMALY_CURRENCY_REFERENCE
    monitor_count: int = 0
    last_evaluated: date | None = None
    stale: bool = False
    anomalies: list[Anomaly] = []
    warnings: list[ReportWarning] = []
    error: CostError | None = None


# ---------- periods, comparison and increase attribution (cost CLI; later MCP tools) ----------

class PeriodKind(StrEnum):
    COMPLETED_MONTH = "completed_month"  # the billing month has ended (AWS may still mark it estimated)
    MONTH_TO_DATE = "month_to_date"  # the current month so far: never a final bill
    PARTIAL_MONTH = "partial_month"  # the first days of a completed month, for like-for-like comparison


class Period(_Frozen):
    kind: PeriodKind
    label: str  # e.g. "September 2026"
    start: date
    end_exclusive: date
    days: int
    days_in_month: int


class ComparisonBasis(StrEnum):
    FULL_MONTHS = "full_months"  # both periods are completed months
    MATCHING_DAYS = "matching_days"  # later month is month to date; earlier month cut to the same number of days


class ServiceChange(_Frozen):
    service: str
    earlier: Decimal  # charge categories only, as in ServiceCost
    later: Decimal
    change: Decimal
    tax_only: bool = False


class CategoryChange(_Frozen):
    category: Category
    earlier: Decimal
    later: Decimal
    change: Decimal


class IncreaseVerdict(StrEnum):
    INCREASE_ATTRIBUTED = "INCREASE_ATTRIBUTED"  # net rose materially; located in named services/adjustments
    NO_INCREASE = "NO_INCREASE"  # net fell materially
    NO_MATERIAL_CHANGE = "NO_MATERIAL_CHANGE"  # |net change| below the materiality threshold
    CANNOT_DETERMINE = "CANNOT_DETERMINE"  # net rose materially, but the evidence can't locate it; no cause stated


class DriverKind(StrEnum):
    SERVICE = "service"  # a service's charges (usage, Savings Plan negation, fees, tax)
    ADJUSTMENT = "adjustment"  # discounts, credits or refunds
    UNCLASSIFIED = "unclassified"  # record types CloudGuard does not recognise


class Driver(_Frozen):
    kind: DriverKind
    name: str  # service name as AWS returned it, or the adjustment category
    change: Decimal  # contribution to the calculated net change
    share_of_gross_increase_percent: Decimal | None = None  # only for positive contributions


class IncreaseExplanation(_Frozen):
    """Where the calculated net change appears. A location, not a cause: CloudGuard sees service and
    charge-type totals, not why usage changed."""

    verdict: IncreaseVerdict
    net_change: Decimal  # exact, never rounded
    gross_increase: Decimal  # sum of every positive contribution, material or not
    drivers: list[Driver] = []  # material positive contributions, largest first
    offsets: list[Driver] = []  # material negative contributions, largest first
    reasons: list[str] = []  # why the verdict is CANNOT_DETERMINE (or caveats)
    materiality_threshold: Decimal = MATERIALITY_THRESHOLD  # in the report currency's units
    below_threshold_change: Decimal = Decimal(0)  # sum of contributions each smaller than the threshold
    limitation: str = ("Shows which services and charge types changed, from Cost Explorer totals. It does not "
                       "show why usage changed; no cause is inferred.")


class RankedService(_Frozen):
    rank: int
    service: str
    amount: Decimal  # charges before discounts, credits and refunds


class ServiceRanking(_Frozen):
    """Services by charges. AWS's tax-only "Tax" line is not ranked but is disclosed in excluded_tax."""

    basis: str
    services: list[RankedService]
    excluded_tax: Decimal | None  # None when there is no data
    total_services: int = 0
    truncated: bool = False  # services beyond the requested limit were left out of this list


class CostComparison(_ReportMeta):
    """Two periods compared. Every figure is a CloudGuard calculation from UnblendedCost lines.

    calculated_net change == sum(service_changes.change) + discount, credit, refund and unclassified changes.
    """

    status: BillStatus
    metric: str = METRIC
    billing_timezone: str = "UTC"
    currency: str | None = None
    basis: ComparisonBasis
    earlier: Period
    later: Period
    earlier_full_month: Period | None = None  # set for MATCHING_DAYS: context only, not compared
    earlier_month: MonthCost | None = None  # the compared earlier period (cut to matching days if so)
    later_month: MonthCost | None = None
    earlier_full_month_cost: MonthCost | None = None
    charges_change: Decimal | None = None
    charges_change_percent: Decimal | None = None  # None when the earlier charges were zero
    net_change: Decimal | None = None
    service_changes: list[ServiceChange] = []  # largest increase first
    category_changes: list[CategoryChange] = []
    explanation: IncreaseExplanation | None = None
    warnings: list[ReportWarning] = []
    error: CostError | None = None
