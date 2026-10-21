"""Read-only cost intelligence MCP tools.

Thin adapters over cloudguard.cost.service: they validate input, run the existing deterministic code in a
worker thread and wrap the existing report models in a stable envelope. No billing arithmetic lives here.
No LLM is involved: `speech` is the deterministic voice.py template.

Every result carries: schema_version, tool, outcome (answered | findings | unavailable), mode,
data_source, as_of, currency, freshness and speech, plus the full evidence report. "unavailable"
means no figures were produced (AWS error, no data, mixed currency, no monitor, ...); it is never
reported as zero spend. Invalid input is a tool error (isError) with a plain message.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from cloudguard.cost import service
from cloudguard.cost.anomalies import DEFAULT_LOOKBACK_DAYS, MAX_LOOKBACK_DAYS
from cloudguard.cost.compare import month_period
from cloudguard.cost.models import (
    SCHEMA_VERSION, AnomalyReport, BillReport, CostComparison, Period, PeriodKind, ReportWarning, ServiceRanking,
)
from cloudguard.cost.voice import anomalies_speech, top_service_speech
from cloudguard.server.context import CostContext

MAX_RANKING = 50
_MONTH_PATTERN = r"^(\d{4}-\d{2}|last-month|this-month|[A-Za-z]{3,9})$"
_MONTH_HELP = "YYYY-MM, an English month name (latest occurrence not after today), last-month or this-month"

Month = Annotated[str, Field(pattern=_MONTH_PATTERN, max_length=10, description=_MONTH_HELP)]

COST_TOOL_NAMES = ("get_monthly_aws_bill", "get_current_month_spending", "get_cost_breakdown",
                   "compare_monthly_costs", "investigate_cost_increase", "get_cost_anomalies")


# ---------- output models ----------

class Freshness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime  # when CloudGuard built the answer
    retrieved_at: datetime | None  # when AWS (or the fixture) last answered; None if it never did
    aws_requests: int  # Cost Explorer requests sent (each is billed in live mode)
    estimated: bool = False  # AWS marks some figures as estimated
    anomaly_last_evaluated: date | None = None
    anomaly_data_stale: bool = False
    note: str = "Cost Explorer refreshes cost data at least once a day; the latest day is usually incomplete."


class _Envelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    tool: str
    outcome: service.Outcome
    mode: Literal["fixture", "live"]
    data_source: str
    as_of: date  # the reference date (UTC) used to resolve "last month", "this month" and month names
    currency: str | None
    freshness: Freshness
    speech: str  # short deterministic reply for a voice client


class MonthlyBillResult(_Envelope):
    is_final_bill: bool  # True for a completed month; AWS may still mark it estimated (see freshness)
    period: Period
    report: BillReport


class CurrentSpendingResult(_Envelope):
    is_final_bill: Literal[False] = False  # month to date is never a bill
    period: Period
    report: BillReport


class CostBreakdownResult(_Envelope):
    is_final_bill: bool
    period: Period
    ranking: ServiceRanking
    report: BillReport


class CostComparisonResult(_Envelope):
    report: CostComparison


class CostAnomaliesResult(_Envelope):
    report: AnomalyReport


# ---------- helpers ----------

def _month(value: str, as_of: date) -> Period:
    try:
        return month_period(service.parse_month(value, as_of), as_of)
    except service.MonthError as exc:
        raise ToolError(str(exc)) from None


def _freshness(report, **extra) -> Freshness:
    return Freshness(generated_at=report.generated_at, retrieved_at=report.retrieved_at,
                     aws_requests=report.aws_requests,
                     estimated=ReportWarning.ESTIMATED in report.warnings, **extra)


def _common(ctx: CostContext, tool: str, as_of: date, report, outcome, speech, **extra) -> dict:
    return dict(tool=tool, outcome=outcome, mode=ctx.mode, data_source=ctx.data_source, as_of=as_of,
                currency=report.currency, freshness=extra.pop("freshness", None) or _freshness(report),
                speech=speech, report=report, **extra)


async def _run(fn, *args):
    """Cost Explorer calls block; keep them off the event loop."""
    return await anyio.to_thread.run_sync(lambda: fn(*args))


def _annotations(ctx: CostContext, title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                           openWorldHint=ctx.mode == "live")


# ---------- tools ----------

def register_cost_tools(server: FastMCP, ctx: CostContext) -> None:
    """Register the six read-only cost tools on `server`, bound to the startup context `ctx`."""

    def month_result(tool: str, period: Period, as_of: date, report: BillReport, speech: str, **extra) -> dict:
        return _common(ctx, tool, as_of, report, service.bill_outcome(report.status), speech,
                       is_final_bill=period.kind == PeriodKind.COMPLETED_MONTH, period=period, **extra)

    @server.tool(annotations=_annotations(ctx, "Monthly AWS bill"), structured_output=True)
    async def get_monthly_aws_bill(month: Month = "last-month") -> MonthlyBillResult:
        """The AWS bill for a completed billing month (UTC): "What was my AWS bill last month?"

        Returns charges before discounts, credits and refunds (UnblendedCost), each category (usage,
        Savings Plan negation, fees, tax, discounts, credits, refunds, unclassified), CloudGuard's
        calculated net (not the AWS invoice amount), cost per service, the RECORD_TYPE evidence and the
        change from the month before. The current month is refused: use get_current_month_spending.
        """
        as_of = ctx.as_of()
        period = _month(month, as_of)
        if period.kind != PeriodKind.COMPLETED_MONTH:
            raise ToolError(f"{period.label} is still in progress, so it has no bill yet; "
                            "use get_current_month_spending")
        report = await _run(service.month_costs, ctx.client, period, ctx.clock(), ctx.max_pages)
        return MonthlyBillResult(**month_result("get_monthly_aws_bill", period, as_of, report,
                                                service.month_speech(report, period)))

    @server.tool(annotations=_annotations(ctx, "Current month spending"), structured_output=True)
    async def get_current_month_spending() -> CurrentSpendingResult:
        """Spending so far in the current AWS billing month (UTC): "How much have I spent this month?"

        Month-to-date figures, never a final bill (is_final_bill is always false). Same categories and
        evidence as get_monthly_aws_bill; no comparison, because a partial month can't be compared with
        a full one (use compare_monthly_costs for a matching-days comparison).
        """
        as_of = ctx.as_of()
        period = _month("this-month", as_of)
        report = await _run(service.month_costs, ctx.client, period, ctx.clock(), ctx.max_pages)
        return CurrentSpendingResult(**_common(ctx, "get_current_month_spending", as_of, report,
                                               service.bill_outcome(report.status),
                                               service.month_speech(report, period), period=period))

    @server.tool(annotations=_annotations(ctx, "Cost breakdown by service"), structured_output=True)
    async def get_cost_breakdown(month: Month = "last-month",
                                 limit: Annotated[int, Field(ge=1, le=MAX_RANKING,
                                                             description="Ranked services to return")] = 10,
                                 ) -> CostBreakdownResult:
        """Costs by AWS service and charge type: "Which AWS service costs me the most?"

        `ranking` lists services by charges before discounts, credits and refunds. AWS's tax-only "Tax"
        line is not ranked but is disclosed as ranking.excluded_tax. `report` keeps every service,
        category and RECORD_TYPE line. The current month is allowed and is labelled month to date.
        """
        as_of = ctx.as_of()
        period = _month(month, as_of)
        report = await _run(service.month_costs, ctx.client, period, ctx.clock(), ctx.max_pages)
        return CostBreakdownResult(**month_result("get_cost_breakdown", period, as_of, report,
                                                  top_service_speech(report, period),
                                                  ranking=service.ranking(report, limit)))

    async def comparison(tool: str, earlier_month: str | None, later_month: str | None, explain: bool) -> dict:
        as_of = ctx.as_of()
        try:
            earlier, later = service.comparison_months(earlier_month, later_month, as_of)
        except service.MonthError as exc:
            raise ToolError(str(exc)) from None
        report = await _run(service.compare, ctx.client, earlier, later, as_of, ctx.clock(), ctx.max_pages)
        return _common(ctx, tool, as_of, report, service.bill_outcome(report.status),
                       service.compare_speech(report, explain=explain))

    @server.tool(annotations=_annotations(ctx, "Compare monthly costs"), structured_output=True)
    async def compare_monthly_costs(earlier_month: Month | None = None,
                                    later_month: Month | None = None) -> CostComparisonResult:
        """Compare two billing months: "Compare September and October."

        Give both months (earlier first) or neither (the last two completed months). If the later month
        is still in progress, basis is "matching_days": the earlier month is cut to the same number of
        days, and its full-month cost is context only. Returns per-category and per-service changes.
        """
        return CostComparisonResult(**await comparison("compare_monthly_costs", earlier_month, later_month, False))

    @server.tool(annotations=_annotations(ctx, "Investigate a cost increase"), structured_output=True)
    async def investigate_cost_increase(earlier_month: Month | None = None,
                                        later_month: Month | None = None) -> CostComparisonResult:
        """Where a cost increase appears: "Why did my AWS costs increase?"

        Splits the change in CloudGuard's calculated net exactly into per-service charge changes plus
        discount, credit, refund and unclassified changes (report.explanation). The verdict is
        INCREASE_ATTRIBUTED (names services/charge types, never a cause), NO_INCREASE,
        NO_MATERIAL_CHANGE (net moved by less than explanation.materiality_threshold, 0.01 in the report
        currency), or CANNOT_DETERMINE (for example when unclassified record types dominate). Only
        contributions of at least the threshold are listed as drivers; the rest are totalled in
        below_threshold_change. It never states why usage changed. Months as in compare_monthly_costs.
        """
        return CostComparisonResult(**await comparison("investigate_cost_increase", earlier_month, later_month,
                                                       True))

    @server.tool(annotations=_annotations(ctx, "Cost anomalies"), structured_output=True)
    async def get_cost_anomalies(lookback_days: Annotated[int, Field(
            ge=1, le=MAX_LOOKBACK_DAYS, description="Days before today (UTC) to search")] = DEFAULT_LOOKBACK_DAYS,
    ) -> CostAnomaliesResult:
        """Unusual charges found by AWS Cost Anomaly Detection: "Are there any unusual charges?"

        report.status tells the cases apart: ANOMALIES_FOUND (outcome "findings"), NO_ANOMALIES (not a
        guarantee that spend is normal), NO_MONITOR, NOT_YET_EVALUATED, ACCESS_DENIED and ERROR (outcome
        "unavailable"). freshness.anomaly_data_stale is true when AWS last evaluated more than 2 days ago.
        Impact amounts are USD per AWS documentation (the API returns no currency field).
        """
        as_of = ctx.as_of()
        report = await _run(service.anomalies, ctx.client, as_of, lookback_days, ctx.clock(), ctx.max_pages)
        fresh = _freshness(report, anomaly_last_evaluated=report.last_evaluated, anomaly_data_stale=report.stale)
        return CostAnomaliesResult(**_common(ctx, "get_cost_anomalies", as_of, report,
                                             service.anomaly_outcome(report.status), anomalies_speech(report),
                                             freshness=fresh))
