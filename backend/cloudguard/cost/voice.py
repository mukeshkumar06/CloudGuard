"""Short spoken summaries of cost reports. Deterministic templates; no account identifiers."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from cloudguard.cost.models import (
    AnomalyReport, AnomalyStatus, BillReport, BillStatus, ComparisonBasis, CostComparison, Driver, DriverKind,
    IncreaseVerdict, MonthCost, Period, PeriodKind, ReportWarning,
)

TOP_SERVICES = 3
_CURRENCY_WORDS = {"USD": "US dollars"}

# Speech tolerance, in currency units. Amounts are spoken to 2 decimal places (ROUND_HALF_UP), so a net
# whose magnitude is below half a cent would be read out as "0.00". Such a net is spoken as approximately
# zero instead of taking a sign from a value too small to say. The sign is always taken from the unrounded
# Decimal, and only the spoken wording uses this tolerance: report amounts are never rounded or changed.
# Assumes a currency with 2 minor-unit decimals (USD, the only currency Cost Explorer has returned so far).
SPEECH_NET_TOLERANCE = Decimal("0.005")


def _amount(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), ROUND_HALF_UP):,}"


def _day(d: date) -> str:
    return f"{d:%B} {d.day}"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"


def negligible(value: Decimal) -> bool:
    """True when `value` (unrounded) is too small to speak: it would read as 0.00."""
    return abs(value) < SPEECH_NET_TOLERANCE


def net_phrase(value: Decimal) -> str:
    """A calculated net said naturally, signed from the unrounded value.

    Positive: "a net charge of X". Negative: "a net credit of X", never "minus". Exactly zero: "a net of
    zero". Non-zero but within SPEECH_NET_TOLERANCE: approximately zero, so a sign is never attached to "0.00".
    """
    if value == 0:
        return "a net of zero"
    if negligible(value):
        return "a net of approximately zero (charges and credits approximately balance)"
    return f"a net credit of {_amount(-value)}" if value < 0 else f"a net charge of {_amount(value)}"


def _unit(currency: str | None) -> str:
    return _CURRENCY_WORDS.get(currency, currency or "")


def top_services(m: MonthCost, limit: int = TOP_SERVICES):
    """Highest-charge services, excluding the tax-only "Tax" line (tax stays in the detailed report)."""
    return [s for s in m.services if not s.tax_only][:limit]


def _detail_sentences(m: MonthCost, *, so_far: bool) -> list[str]:
    """Adjustments and net, unclassified warning and top services, shared by bill and month-to-date replies."""
    parts = []
    adjustments = [f"{label} of {_amount(-value)}" for label, value in
                   (("discounts", m.discounts), ("credits", m.credits), ("refunds", m.refunds)) if value]
    if adjustments or m.unclassified_record_types:
        lead = f"After {_and(adjustments)}, " if adjustments else ""
        parts.append(f"{lead}CloudGuard calculates {net_phrase(m.calculated_net)}{' so far' if so_far else ''}. "
                     "That's a CloudGuard calculation and may differ from your invoice.")
    if m.unclassified_record_types:
        n = len(m.unclassified_record_types)
        what = "1 type of billing entry" if n == 1 else f"{n} types of billing entries"
        total = "It totals" if n == 1 else "Together they total"
        parts.append(f"Warning: {what} could not be classified. {total} {_amount(m.unclassified)}. "
                     "Unclassified amounts are not counted as charges but are included in CloudGuard's net.")
    # Tax stays in the structured report and the totals; it is not a service worth naming here.
    top = top_services(m)
    if top:
        if so_far:
            noun = "service so far is" if len(top) == 1 else "services so far are"
        else:
            noun = "service was" if len(top) == 1 else "services were"
        parts.append(f"The top {noun} {_and([f'{s.service} at {_amount(s.amount)}' for s in top])}.")
    return parts


def bill_failure_speech(report: BillReport | CostComparison, period: str) -> str | None:
    """The spoken reply for a non-OK bill or comparison status, or None when the status is OK."""
    match report.status:
        case BillStatus.NO_DATA:
            return f"AWS Cost Explorer returned no cost data for {period}."
        case BillStatus.DATA_UNAVAILABLE:
            return ("AWS reports that the requested cost data is unavailable. Possible causes include "
                    "Cost Explorer not being enabled yet, or AWS still preparing the data.")
        case BillStatus.ACCESS_DENIED:
            return "I don't have permission to read billing data from AWS Cost Explorer."
        case BillStatus.MIXED_CURRENCY:
            return "Cost Explorer returned amounts in more than one currency, so I won't add them together."
        case BillStatus.ERROR:
            return "I couldn't retrieve your AWS bill right now."
    return None


def bill_speech(report: BillReport) -> str:
    month = f"{report.period_start:%B %Y}"
    if (failed := bill_failure_speech(report, f"the {month} billing month")) is not None:
        return failed

    m = report.month
    parts = [f"For the {month} AWS billing month, charges before discounts, credits and refunds were "
             f"{_amount(m.charges_before_discounts_credits_and_refunds)} {_unit(report.currency)}, "
             "measured as unblended cost."]
    parts += _detail_sentences(m, so_far=False)
    c = report.comparison
    if c is not None:
        prev = f"{c.previous_start:%B}"
        if c.change == 0:
            parts.append(f"That's the same as {prev}.")
        elif negligible(c.change):
            parts.append(f"That's about the same as {prev}, less than 0.01 apart.")
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


# ---------- cost CLI questions (month to date, top service, comparison, increase) ----------

_LAG = "Cost Explorer updates at least once a day, so the most recent charges may not be included yet."
_ESTIMATED = "AWS still marks some of these figures as estimated, so they may change."


def _through(period: Period) -> str:
    return f"from {_day(period.start)} to {_day(period.end_exclusive - timedelta(days=1))}"


def month_to_date_speech(report: BillReport, period: Period) -> str:
    """Current-month spending so far. Never called a bill: AWS finalises the bill after the month ends."""
    if (failed := bill_failure_speech(report, f"{period.label} so far")) is not None:
        return failed
    m = report.month
    parts = [f"So far in {period.label}, {_through(period)}, AWS charges before discounts, credits and refunds "
             f"are {_amount(m.charges_before_discounts_credits_and_refunds)} {_unit(report.currency)}, measured as "
             "unblended cost. This is month-to-date spending, not a final bill."]
    parts += _detail_sentences(m, so_far=True)
    parts.append(_LAG)
    return " ".join(parts)


def top_service_speech(report: BillReport, period: Period) -> str:
    completed = period.kind == PeriodKind.COMPLETED_MONTH
    what = f"the {period.label} billing month" if completed else f"{period.label} so far"
    if (failed := bill_failure_speech(report, what)) is not None:
        return failed
    when, verb, rest = ((f"In {period.label}", "was", "were") if completed
                        else (f"So far in {period.label}", "is", "are"))
    top = top_services(report.month)
    if not top:
        return f"{when}, no AWS service had charges apart from tax."
    first = top[0]
    text = (f"{when}, your highest-cost AWS service {verb} {first.service} at {_amount(first.amount)} "
            f"{_unit(report.currency)}, measured as unblended cost before discounts, credits and refunds.")
    if len(top) > 1:
        text += f" Next {rest} {_and([f'{s.service} at {_amount(s.amount)}' for s in top[1:]])}."
    if not completed:
        text += " These are month-to-date figures, not a final bill."
    return text


def _missing(c: CostComparison) -> str:
    gone = [p.label for p, m in ((c.earlier, c.earlier_month), (c.later, c.later_month)) if m is None]
    return _and(gone or [c.earlier.label, c.later.label])


def _span(c: CostComparison) -> str:
    if c.basis == ComparisonBasis.FULL_MONTHS:
        return f"from {c.earlier.label} to {c.later.label}"
    return (f"over the first {c.later.days} days of {c.later.label}, compared with the first "
            f"{c.earlier.days} days of {c.earlier.label}")


def comparison_speech(c: CostComparison) -> str:
    if c.status == BillStatus.NO_DATA:
        return f"AWS Cost Explorer returned no cost data for {_missing(c)}, so I can't compare the two months."
    if (failed := bill_failure_speech(c, "")) is not None:
        return failed
    e, l, unit = c.earlier_month, c.later_month, _unit(c.currency)
    charges = "charges before discounts, credits and refunds"
    if c.basis == ComparisonBasis.FULL_MONTHS:
        text = (f"In {c.later.label}, {charges} were {_amount(l.charges_before_discounts_credits_and_refunds)} "
                f"{unit}, compared with {_amount(e.charges_before_discounts_credits_and_refunds)} in {c.earlier.label}")
    else:
        text = (f"{c.later.label} is still in progress, so this compares matching days. For the first "
                f"{c.later.days} days, {charges} are {_amount(l.charges_before_discounts_credits_and_refunds)} "
                f"{unit}, compared with {_amount(e.charges_before_discounts_credits_and_refunds)} for the first "
                f"{c.earlier.days} days of {c.earlier.label}")
    if c.charges_change == 0:
        text += ", no change."
    elif negligible(c.charges_change):
        text += ", about the same (less than 0.01 apart)."
    else:
        up = c.charges_change > 0
        pct = ("" if c.charges_change_percent is None
               else f", {'up' if up else 'down'} {abs(c.charges_change_percent)} percent")
        text += f": {_amount(abs(c.charges_change))} {'more' if up else 'less'}{pct}."
    parts = [text]
    if e.calculated_net != e.charges_before_discounts_credits_and_refunds or \
            l.calculated_net != l.charges_before_discounts_credits_and_refunds:
        if negligible(l.calculated_net) and negligible(e.calculated_net):
            parts.append("After discounts, credits and refunds, charges and credits approximately balance in both "
                         "periods, so CloudGuard's calculated net is approximately zero for each.")
        else:
            parts.append(f"After discounts, credits and refunds, CloudGuard calculates {net_phrase(l.calculated_net)}, "
                         f"against {net_phrase(e.calculated_net)} before.")
    if c.earlier_full_month_cost is not None:
        parts.append(f"For context, {c.earlier.label}'s full-month charges were "
                     f"{_amount(c.earlier_full_month_cost.charges_before_discounts_credits_and_refunds)}.")
    if ReportWarning.ESTIMATED in c.warnings:
        parts.append(_ESTIMATED)
    return " ".join(parts)


def _driver(d: Driver) -> str:
    match d.kind:
        case DriverKind.ADJUSTMENT:
            return f"{d.name} being {_amount(d.change)} smaller"
        case DriverKind.UNCLASSIFIED:
            return f"unclassified billing entries, up {_amount(d.change)}"
    return f"{'tax' if d.name == 'Tax' else d.name}, up {_amount(d.change)}"


def increase_speech(c: CostComparison) -> str:
    """Where an increase appears. Never states a cause: CloudGuard sees totals, not why usage changed."""
    if c.status == BillStatus.NO_DATA:
        return f"I can't determine why your costs changed: AWS Cost Explorer returned no cost data for {_missing(c)}."
    if (failed := bill_failure_speech(c, "")) is not None:
        return f"I can't determine why your costs changed. {failed}"
    x, unit, span = c.explanation, _unit(c.currency), _span(c)
    threshold = _amount(x.materiality_threshold)
    charges_rose = c.charges_change >= x.materiality_threshold
    match x.verdict:
        case IncreaseVerdict.NO_MATERIAL_CHANGE:
            text = ("Your costs didn't increase: CloudGuard's calculated net is unchanged " + span + "."
                    if x.net_change == 0 else
                    f"Your costs didn't change materially: CloudGuard's calculated net moved by less than "
                    f"{threshold} {unit} {span}.")
            if charges_rose:
                text += (f" Charges before discounts, credits and refunds did rise by {_amount(c.charges_change)}, "
                         "but discounts, credits or refunds offset them.")
        case IncreaseVerdict.NO_INCREASE:
            text = (f"Your costs didn't increase: CloudGuard's calculated net fell by {_amount(-x.net_change)} "
                    f"{unit} {span}.")
            if charges_rose:
                text += (f" Charges before discounts, credits and refunds did rise by {_amount(c.charges_change)}, "
                         "but other adjustments offset them.")
        case IncreaseVerdict.CANNOT_DETERMINE:
            text = (f"CloudGuard's calculated net rose by {_amount(x.net_change)} {unit} {span}, but I can't "
                    f"determine where most of the increase came from: {x.reasons[0]}.")
        case _:
            text = (f"CloudGuard's calculated net rose by {_amount(x.net_change)} {unit} {span}. "
                    f"The largest change was {_driver(x.drivers[0])}")
            if len(x.drivers) > 1:
                text += f", followed by {_driver(x.drivers[1])}"
            text += (". I can see which services and charge types changed, but not why, so I can't say what "
                     "caused it.")
    if ReportWarning.ESTIMATED in c.warnings:
        text += " " + _ESTIMATED
    return text
