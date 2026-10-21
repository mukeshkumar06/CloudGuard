"""Human-readable cost reports for the CLI's `--format summary`. Deterministic text; no account identifiers.

The detailed report discloses everything the spoken reply leaves out: tax, fees, every adjustment,
unclassified record types and the RECORD_TYPE evidence behind each figure.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from cloudguard.cost.models import (
    NET_BASIS, AnomalyReport, AnomalyStatus, BillReport, BillStatus, ComparisonBasis, CostComparison, CostError,
    DriverKind, MonthCost, Period, PeriodKind, ReportWarning,
)

_W = 52  # label column width

_KIND_TITLE = {
    PeriodKind.COMPLETED_MONTH: "Completed-month bill (CloudGuard calculation from Cost Explorer)",
    PeriodKind.MONTH_TO_DATE: "Month-to-date spending - NOT a final bill",
    PeriodKind.PARTIAL_MONTH: "Partial month",
}

_NOTES = [
    ("fees", "Fees: support, reservation and Savings Plan fees. Counted as charges."),
    ("tax", "Tax: AWS reports tax as its own \"Tax\" line. Counted in charges and the net; excluded from "
            "spoken service rankings."),
    ("savings_plan_negation", "Savings Plan negation: offsets usage a Savings Plan covered, which Cost Explorer "
                              "shows at on-demand rates. The plan's own cost appears as a fee."),
    ("discounts", "Discounts: price reductions AWS applied. Negative amounts; not in service totals."),
    ("credits", "Credits: AWS promotional or service credits. Negative amounts; they lower the net, not the "
                "charges, and are not in service totals."),
    ("refunds", "Refunds: amounts AWS returned for earlier charges. Negative amounts; not in service totals."),
]

_WARNINGS = {
    ReportWarning.ESTIMATED: "AWS marks some figures as estimated; they may change until AWS finalises the bill.",
    ReportWarning.UNCLASSIFIED_RECORD_TYPES: "Some record types are not recognised; see Unclassified.",
    ReportWarning.CURRENCY_MISMATCH: "The anomaly currency differs from the bill currency; nothing was converted.",
    ReportWarning.STALE_EVALUATION: "AWS last evaluated anomalies more than 2 days ago; recent spend may be missing.",
    ReportWarning.MONTH_TO_DATE: "The later period is the current month to date, not a final bill.",
}


def _cents(value: Decimal) -> Decimal:
    rounded = value.quantize(Decimal("0.01"), ROUND_HALF_UP)
    return abs(rounded) if rounded == 0 else rounded  # never print "-0.00"


def money(value: Decimal | None) -> str:
    """Display only: 2 decimal places. Report data keeps the exact Decimal."""
    return "-" if value is None else f"{_cents(value):,}"


def _signed(value: Decimal) -> str:
    return ("+" if _cents(value) > 0 else "") + money(value)


def _net_note(value: Decimal) -> str:
    if value != 0 and _cents(value) == 0:
        return f"(approximately zero; exact {value:f})"
    return "(net credit)" if value < 0 else ""


def _pct(value: Decimal) -> str:
    return f"{value:+}%"


def _row(label: str, value: Decimal | None, note: str = "") -> str:
    return f"  {label:<{_W}}{money(value):>12}" + (f"  {note}" if note else "")


def _period_line(p: Period) -> str:
    return (f"{p.label}: {p.start.isoformat()} to {(p.end_exclusive - timedelta(days=1)).isoformat()} inclusive "
            f"({p.days} of {p.days_in_month} days, UTC)")


def _source(report, source: str) -> list[str]:
    lines = [f"Source:    {source}; Cost Explorer requests sent: {report.aws_requests}"]
    if report.retrieved_at:
        lines.append(f"Retrieved: {report.retrieved_at.isoformat()}")
    return lines


def _error(error: CostError | None) -> list[str]:
    if error is None:
        return []
    lines = ["", f"AWS error: {error.code} ({error.operation})"]
    if error.aws_description:
        lines.append(f"  AWS says: {error.aws_description}")
    for cause in error.inferred_possible_causes:
        lines.append(f"  Possible cause (CloudGuard's inference, not AWS's): {cause}")
    return lines


def _warnings(warnings) -> list[str]:
    return (["", "Warnings:"] + [f"  - {w.value}: {_WARNINGS.get(w, w.value)}" for w in warnings]) if warnings else []


def _breakdown(m: MonthCost, currency: str | None) -> list[str]:
    lines = [f"Breakdown ({currency})",
             _row("Usage", m.usage), _row("Savings Plan negation", m.savings_plan_negation),
             _row("Fees", m.fees), _row("Tax", m.tax),
             _row("= Charges before discounts, credits and refunds", m.charges_before_discounts_credits_and_refunds),
             _row("Discounts", m.discounts), _row("Credits", m.credits), _row("Refunds", m.refunds),
             _row("Unclassified", m.unclassified),
             _row("= CloudGuard calculated net", m.calculated_net, _net_note(m.calculated_net)),
             f"  ({NET_BASIS})"]
    notes = [text for field, text in _NOTES if getattr(m, field)]
    if m.unclassified_record_types:
        notes.append(f"Unclassified: record types CloudGuard does not recognise "
                     f"({', '.join(m.unclassified_record_types)}). Not counted as charges; included in the net.")
    if notes:
        lines += ["", "What the adjustments mean:"] + [f"  - {n}" for n in notes]
    return lines


def _services(m: MonthCost, currency: str | None) -> list[str]:
    lines = [f"Cost by service ({currency}; charges only: usage, Savings Plan negation, fees, tax)"]
    rank = 0
    for s in m.services:
        if s.tax_only:
            continue
        rank += 1
        lines.append(_row(f"{rank}. {s.service}", s.amount))
    tax = [s for s in m.services if s.tax_only]
    for s in tax:
        lines.append(_row(f"   {s.service} (not ranked as a service)", s.amount))
    if not m.services:
        lines.append("  (no service charges)")
    return lines


def _evidence(m: MonthCost) -> list[str]:
    lines = ["Evidence: RECORD_TYPE totals returned by Cost Explorer"]
    for r in m.record_types:
        source = r.match_source.value if r.match_source else "unmatched"
        lines.append(f"  {r.record_type[:28]:<28} {r.category.value:<22} {source:<12}{money(r.amount):>12}")
    return lines


def render_bill(report: BillReport, period: Period, source: str, *, focus_services: bool = False) -> str:
    title = _KIND_TITLE[period.kind] if not focus_services else "Top AWS services by cost"
    lines = [f"CloudGuard cost report: {title}", f"Period:    {_period_line(period)}",
             f"Status:    {report.status.value}", f"Currency:  {report.currency or 'not reported'}; "
             f"metric: {report.metric}"] + _source(report, source)
    if period.kind == PeriodKind.MONTH_TO_DATE:
        lines.append("Note:      month-to-date spending. AWS finalises the bill after the month ends; "
                     "the latest day's data is usually incomplete.")
    m = report.month
    if report.status == BillStatus.OK and m is not None:
        sections = [_services(m, report.currency), _breakdown(m, report.currency)] if focus_services else \
            [_breakdown(m, report.currency), _services(m, report.currency)]
        for section in sections + [_evidence(m)]:
            lines += [""] + section
        c = report.comparison
        if c is not None:
            pct = "" if c.change_percent is None else f" ({_pct(c.change_percent)})"
            lines += ["", f"Compared with {c.previous_start:%B %Y}: charges {money(c.previous_charges)} -> "
                          f"{money(m.charges_before_discounts_credits_and_refunds)}, change {_signed(c.change)}{pct}"]
    elif report.status == BillStatus.NO_DATA:
        lines += ["", "Cost Explorer returned no cost lines for this period. This is not reported as zero spend."]
    elif report.status == BillStatus.MIXED_CURRENCY:
        lines += ["", "Cost Explorer returned more than one currency; amounts were not added together."]
    return "\n".join(lines + _warnings(report.warnings) + _error(report.error))


def render_comparison(c: CostComparison, source: str, *, explain: bool = False) -> str:
    title = "Why did costs change?" if explain else "Month comparison"
    lines = [f"CloudGuard cost report: {title}",
             f"Earlier:   {_period_line(c.earlier)}" + (" [cut to matching days]" if c.earlier_full_month else ""),
             f"Later:     {_period_line(c.later)}" + (" [month to date - NOT a final bill]"
                                                       if c.later.kind == PeriodKind.MONTH_TO_DATE else ""),
             f"Basis:     {c.basis.value}" + (" (later month in progress; earlier month cut to the same number "
                                              "of days)" if c.basis == ComparisonBasis.MATCHING_DAYS else ""),
             f"Status:    {c.status.value}", f"Currency:  {c.currency or 'not reported'}; metric: {c.metric}",
             ] + _source(c, source)
    e, l = c.earlier_month, c.later_month
    if c.status == BillStatus.OK:
        lines += ["", f"  {'Totals (' + str(c.currency) + ')':<32}{'earlier':>12}{'later':>12}{'change':>12}"]
        for cat in c.category_changes:
            lines.append(f"  {cat.category.value:<32}{money(cat.earlier):>12}{money(cat.later):>12}"
                         f"{_signed(cat.change):>12}")
        lines.append(f"  {'charges before adjustments':<32}{money(e.charges_before_discounts_credits_and_refunds):>12}"
                     f"{money(l.charges_before_discounts_credits_and_refunds):>12}{_signed(c.charges_change):>12}"
                     + ("" if c.charges_change_percent is None else f"  ({_pct(c.charges_change_percent)})"))
        lines.append(f"  {'calculated net':<32}{money(e.calculated_net):>12}{money(l.calculated_net):>12}"
                     f"{_signed(c.net_change):>12}")
        lines.append(f"  ({NET_BASIS})")
        if c.earlier_full_month_cost is not None:
            lines.append(f"  Context: {c.earlier.label} full-month charges were "
                         f"{money(c.earlier_full_month_cost.charges_before_discounts_credits_and_refunds)} (not compared).")
        lines += ["", f"  {'Service charges (' + str(c.currency) + ')':<44}{'earlier':>12}{'later':>12}{'change':>12}"]
        for s in c.service_changes:
            name = s.service + (" (tax)" if s.tax_only and s.service != "Tax" else "")
            lines.append(f"  {name[:44]:<44}{money(s.earlier):>12}{money(s.later):>12}{_signed(s.change):>12}")
        x = c.explanation
        lines += ["", f"Attribution: {x.verdict.value}",
                  f"  Net change {_signed(x.net_change)} (exact {x.net_change:f}); gross increase "
                  f"{money(x.gross_increase)}",
                  f"  Materiality threshold {x.materiality_threshold} {c.currency}: smaller changes are not "
                  f"listed as drivers (their total: {x.below_threshold_change:f})"]
        for label, items in (("Increased", x.drivers), ("Decreased", x.offsets)):
            for d in items:
                what = {DriverKind.SERVICE: "service charges", DriverKind.ADJUSTMENT: "smaller (less negative)"
                        if d.change > 0 else "larger (more negative)", DriverKind.UNCLASSIFIED: "unclassified"}[d.kind]
                share = ("" if d.share_of_gross_increase_percent is None
                         else f"  {d.share_of_gross_increase_percent}% of gross increase")
                lines.append(f"  {label}: {d.name} [{what}] {_signed(d.change)}{share}")
        for reason in x.reasons:
            lines.append(f"  Cannot determine: {reason}")
        lines.append(f"  Limitation: {x.limitation}")
    elif c.status == BillStatus.NO_DATA:
        missing = [p.label for p, m in ((c.earlier, e), (c.later, l)) if m is None]
        lines += ["", f"Cost Explorer returned no cost lines for {', '.join(missing)}. Not reported as zero spend."]
    elif c.status == BillStatus.MIXED_CURRENCY:
        lines += ["", "Cost Explorer returned more than one currency; amounts were not compared."]
    return "\n".join(lines + _warnings(c.warnings) + _error(c.error))


def render_anomalies(report: AnomalyReport, source: str) -> str:
    lines = ["CloudGuard cost report: Unusual charges (AWS Cost Anomaly Detection)",
             f"Window:    {report.window_start.isoformat()} to {report.window_end.isoformat()} (UTC)",
             f"Status:    {report.status.value}",
             f"Currency:  {report.currency} ({report.currency_basis})",
             f"Monitors:  {report.monitor_count}; last evaluated: "
             f"{report.last_evaluated.isoformat() if report.last_evaluated else 'never'}"
             + ("  [STALE]" if report.stale else "")] + _source(report, source)
    match report.status:
        case AnomalyStatus.NO_MONITOR:
            lines += ["", "No anomaly monitor exists, so AWS is not checking this account for anomalies. "
                          "This is not the same as 'no anomalies'."]
        case AnomalyStatus.NOT_YET_EVALUATED:
            lines += ["", "A monitor exists but AWS has not evaluated it yet; there are no results to report."]
        case AnomalyStatus.NO_ANOMALIES:
            lines += ["", "AWS reported no anomalies in the window. Detection covers only what the monitors watch "
                          "and is not real-time, so this does not guarantee spending is normal."]
    for i, a in enumerate(report.anomalies, 1):
        lines += ["", f"{i}. {a.service or 'Unknown service'} ({a.region or 'unknown region'}, "
                      f"{a.usage_type or 'unknown usage type'})",
                  f"   {a.start_date} to {a.end_date or 'ongoing'}; expected {money(a.expected_spend)}, actual "
                  f"{money(a.actual_spend)}, impact {money(a.impact)} {report.currency}"
                  + ("" if a.impact_percent is None else f" ({a.impact_percent}%)") + f"; score {a.score}",
                  f"   {a.explanation}"]
    return "\n".join(lines + _warnings(report.warnings) + _error(report.error))
