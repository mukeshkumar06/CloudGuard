"""Findings published by AWS Cost Anomaly Detection for a recent window.

Monitors are read first: with no monitor, or one AWS has never evaluated, "no anomalies" would be
meaningless, so those are reported as their own statuses. Detection is not real-time; the latest
monitor evaluation date is reported as the freshness signal.

Currency: the API returns impact amounts without a unit. AWS documents them as dollar values, so
they are labelled USD with that basis exposed in the result (models.ANOMALY_CURRENCY_BASIS). If the
caller passes a bill currency that differs, a CURRENCY_MISMATCH warning is added; nothing is converted.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from botocore.exceptions import BotoCoreError, ClientError

from cloudguard.cost.ce import PageLimitExceeded, RequestLog, paginate, reduce_error, resolve_max_pages
from cloudguard.cost.models import ANOMALY_CURRENCY, Anomaly, AnomalyReport, AnomalyStatus, ReportWarning
from cloudguard.cost.periods import utc_date

DEFAULT_LOOKBACK_DAYS = 7
MAX_LOOKBACK_DAYS = 90
STALE_AFTER = timedelta(days=2)

_AWS_ERRORS = (ClientError, BotoCoreError, PageLimitExceeded)


def _date(value: str | None) -> date | None:
    """AWS returns 'YYYY-MM-DD' or a full ISO timestamp here; only the date is used."""
    return date.fromisoformat(value[:10]) if value else None


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), ROUND_HALF_UP):,}"


def _explain(service: str | None, expected: Decimal | None, actual: Decimal | None, impact: Decimal,
             percent: Decimal | None, end: date | None) -> str:
    who = service or "An AWS service"
    if actual is not None and expected is not None:
        text = f"{who} cost {_money(actual)} USD against an expected {_money(expected)} USD"
    else:
        text = f"{who} cost more than AWS expected"
    text += f", {_money(impact)} USD above the baseline"
    if percent is not None:
        text += f" ({percent.quantize(Decimal('0.1'), ROUND_HALF_UP)}% higher)"
    elif expected == 0:
        text += " (AWS gives no percentage because expected spend was zero)"
    return text + (". AWS still reports it as ongoing." if end is None else ".")


def _parse(raw: dict) -> Anomaly:
    causes = raw.get("RootCauses") or []
    top = max(causes, key=lambda c: (c.get("Impact") or {}).get("Contribution", 0), default={})
    impact_raw = raw["Impact"]
    impact = _dec(impact_raw.get("TotalImpact"))
    if impact is None:
        impact = _dec(impact_raw["MaxImpact"])
    expected = _dec(impact_raw.get("TotalExpectedSpend"))
    actual = _dec(impact_raw.get("TotalActualSpend"))
    percent = _dec(impact_raw.get("TotalImpactPercentage"))
    end = _date(raw.get("AnomalyEndDate"))
    return Anomaly(
        service=top.get("Service"), region=top.get("Region"), usage_type=top.get("UsageType"),
        start_date=_date(raw.get("AnomalyStartDate")), end_date=end,
        expected_spend=expected, actual_spend=actual, impact=impact, impact_percent=percent,
        score=_dec(raw["AnomalyScore"]["MaxScore"]),
        explanation=_explain(top.get("Service"), expected, actual, impact, percent, end),
    )


def get_cost_anomalies(client, now: datetime | None = None, lookback_days: int = DEFAULT_LOOKBACK_DAYS, *,
                       max_pages: int | None = None, bill_currency: str | None = None,
                       clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> AnomalyReport:
    if not 1 <= lookback_days <= MAX_LOOKBACK_DAYS:
        raise ValueError(f"lookback_days must be between 1 and {MAX_LOOKBACK_DAYS}")
    pages = resolve_max_pages(max_pages)
    now = now or clock()
    today = utc_date(now)
    window_start = today - timedelta(days=lookback_days)
    log = RequestLog(clock=clock)
    base_warnings = ([ReportWarning.CURRENCY_MISMATCH]
                     if bill_currency is not None and bill_currency != ANOMALY_CURRENCY else [])

    def report(**fields) -> AnomalyReport:
        warnings = base_warnings + ([ReportWarning.STALE_EVALUATION] if fields.get("stale") else [])
        return AnomalyReport(window_start=window_start, window_end=today, generated_at=now,
                             retrieved_at=log.last_success_at, aws_requests=log.count, warnings=warnings, **fields)

    def failed(exc: Exception, operation: str, **extra) -> AnomalyReport:
        error, denied = reduce_error(exc, operation)
        return report(status=AnomalyStatus.ACCESS_DENIED if denied else AnomalyStatus.ERROR, error=error, **extra)

    try:
        monitors = paginate(client.get_anomaly_monitors, "AnomalyMonitors", log, pages)
    except _AWS_ERRORS as exc:
        return failed(exc, "ce:GetAnomalyMonitors")
    if not monitors:
        return report(status=AnomalyStatus.NO_MONITOR)

    evaluated = [d for d in (_date(m.get("LastEvaluatedDate")) for m in monitors) if d]
    info = dict(monitor_count=len(monitors), last_evaluated=max(evaluated, default=None))
    if not evaluated:
        return report(status=AnomalyStatus.NOT_YET_EVALUATED, **info)
    info["stale"] = today - info["last_evaluated"] > STALE_AFTER

    try:
        raw = paginate(client.get_anomalies, "Anomalies", log, pages,
                       DateInterval={"StartDate": window_start.isoformat(), "EndDate": today.isoformat()})
    except _AWS_ERRORS as exc:
        return failed(exc, "ce:GetAnomalies", **info)

    anomalies = sorted((_parse(a) for a in raw), key=lambda a: -a.impact)
    status = AnomalyStatus.ANOMALIES_FOUND if anomalies else AnomalyStatus.NO_ANOMALIES
    return report(status=status, anomalies=anomalies, **info)
