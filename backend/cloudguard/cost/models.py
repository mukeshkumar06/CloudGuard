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
