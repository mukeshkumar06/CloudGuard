"""Short spoken summaries of cost reports. Deterministic templates; no account identifiers."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from cloudguard.cost.models import AnomalyReport, AnomalyStatus, BillReport, BillStatus, ReportWarning

TOP_SERVICES = 3
_CURRENCY_WORDS = {"USD": "US dollars"}


def _amount(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), ROUND_HALF_UP):,}"


def _day(d: date) -> str:
    return f"{d:%B} {d.day}"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"


def bill_speech(report: BillReport) -> str:
    month = f"{report.period_start:%B %Y}"
    match report.status:
        case BillStatus.NO_DATA:
            return f"AWS Cost Explorer returned no cost data for the {month} billing month."
        case BillStatus.DATA_UNAVAILABLE:
            return ("AWS reports that the requested cost data is unavailable. Possible causes include "
                    "Cost Explorer not being enabled yet, or AWS still preparing the data.")
        case BillStatus.ACCESS_DENIED:
            return "I don't have permission to read billing data from AWS Cost Explorer."
        case BillStatus.MIXED_CURRENCY:
            return "Cost Explorer returned amounts in more than one currency, so I won't add them together."
        case BillStatus.ERROR:
            return "I couldn't retrieve your AWS bill right now."

    m = report.month
    unit = _CURRENCY_WORDS.get(report.currency, report.currency)
    parts = [f"For the {month} AWS billing month, charges before discounts, credits and refunds were "
             f"{_amount(m.charges_before_discounts_credits_and_refunds)} {unit}, measured as unblended cost."]
    adjustments = [f"{label} of {_amount(-value)}" for label, value in
                   (("discounts", m.discounts), ("credits", m.credits), ("refunds", m.refunds)) if value]
    if adjustments or m.unclassified_record_types:
        lead = f"After {_and(adjustments)}, " if adjustments else ""
        parts.append(f"{lead}CloudGuard calculates a net of {_amount(m.calculated_net)}. "
                     "That's a CloudGuard calculation and may differ from your invoice.")
    if m.unclassified_record_types:
        n = len(m.unclassified_record_types)
        what = "1 type of billing entry" if n == 1 else f"{n} types of billing entries"
        total = "It totals" if n == 1 else "Together they total"
        parts.append(f"Warning: {what} could not be classified. {total} {_amount(m.unclassified)}. "
                     "Unclassified amounts are not counted as charges but are included in CloudGuard's net.")
    # Tax stays in the structured report and the totals; it is not a service worth naming here.
    top = [s for s in m.services if not s.tax_only][:TOP_SERVICES]
    if top:
        noun = "service was" if len(top) == 1 else "services were"
        parts.append(f"The top {noun} {_and([f'{s.service} at {_amount(s.amount)}' for s in top])}.")
    c = report.comparison
    if c is not None:
        prev = f"{c.previous_start:%B}"
        if c.change == 0:
            parts.append(f"That's the same as {prev}.")
        else:
            up = c.change > 0
            pct = "" if c.change_percent is None else f", {'up' if up else 'down'} {abs(c.change_percent)} percent"
            parts.append(f"That's {_amount(abs(c.change))} {'more' if up else 'less'} than {prev}{pct}.")
    if ReportWarning.ESTIMATED in report.warnings:
        parts.append("AWS still marks these figures as estimated, so they may change.")
    return " ".join(parts)


def anomalies_speech(report: AnomalyReport) -> str:
    window = f"between {_day(report.window_start)} and {_day(report.window_end)}"
    freshness = f" AWS last evaluated your costs on {_day(report.last_evaluated)}." if report.last_evaluated else ""
    if report.stale:
        freshness += " That's more than two days ago, so recent spending may not be covered."
    match report.status:
        case AnomalyStatus.NO_MONITOR:
            return "No Cost Anomaly Detection monitor is set up, so AWS isn't checking this account for anomalies."
        case AnomalyStatus.NOT_YET_EVALUATED:
            return "Your anomaly monitor hasn't been evaluated by AWS yet, so there are no results to report."
        case AnomalyStatus.ACCESS_DENIED:
            return "I don't have permission to read AWS Cost Anomaly Detection results."
        case AnomalyStatus.ERROR:
            return "I couldn't retrieve cost anomaly results right now."
        case AnomalyStatus.NO_ANOMALIES:
            return (f"AWS Cost Anomaly Detection reported no anomalies {window}. That doesn't guarantee "
                    f"spending is normal: detection covers only what your monitors watch, and it isn't real-time."
                    + freshness)
    n = len(report.anomalies)
    head = (f"AWS Cost Anomaly Detection reported {n} {'anomaly' if n == 1 else 'anomalies'} {window}. "
            f"{'The largest: ' if n > 1 else ''}{report.anomalies[0].explanation}")
    return head + freshness
