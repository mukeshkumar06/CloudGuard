"""Read-only Cost Intelligence CLI: test CloudGuard's cost answers before they are exposed over MCP.

    python -m cloudguard.cost bill          [--period last-month|this-month | --month M]   # Q1, Q2
    python -m cloudguard.cost top-services  [--period last-month|this-month | --month M]   # Q4
    python -m cloudguard.cost compare       [--months EARLIER LATER]                       # Q3
    python -m cloudguard.cost explain-increase [--months EARLIER LATER]                    # Q5
    python -m cloudguard.cost anomalies     [--lookback-days N]                            # Q6

Common options: --mode fixture|live (default fixture), --as-of YYYY-MM-DD, --format summary|json|speech,
--fixture NAME|PATH (fixture mode), --profile NAME (live mode), --max-pages N.
A month M is YYYY-MM, a month name ("september": its latest occurrence not after --as-of), last-month or
this-month. The current month is always reported as month to date, never as a bill. compare and
explain-increase default to the last two completed months.

Fixture mode reads synthetic JSON from backend/fixtures/cost/ (default demo.json; its as_of is the
default --as-of) and never imports boto3, needs no credentials and makes no network call. Live mode is
read-only: GetCostAndUsage, GetAnomalyMonitors and GetAnomalies against the us-east-1 Cost Explorer
endpoint. Every Cost Explorer request is billed (about USD 0.01 each).

Exit codes:
    0  success: the question was answered (including "no anomalies" and "cause cannot be determined")
    1  findings: AWS Cost Anomaly Detection reported anomalies
    2  invalid arguments (bad month, future period, unknown fixture name, ...)
    3  AWS/API error or data unavailable: access denied, throttling, DataUnavailable, no cost data,
       mixed currencies, no anomaly monitor, monitor never evaluated, credentials or profile problems
    4  internal error (including a malformed fixture file)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime

from botocore.exceptions import BotoCoreError

from cloudguard.cost import service
from cloudguard.cost.anomalies import DEFAULT_LOOKBACK_DAYS, MAX_LOOKBACK_DAYS
from cloudguard.cost.ce import CE_REGION, MAX_PAGES_LIMIT, resolve_max_pages
from cloudguard.cost.compare import month_period
from cloudguard.cost.fixture import FixtureCostExplorer, FixtureError, resolve_fixture
from cloudguard.cost.models import SCHEMA_VERSION
from cloudguard.cost.summary import render_anomalies, render_bill, render_comparison
from cloudguard.cost.voice import anomalies_speech, top_service_speech

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_INTERNAL = 4

class UsageError(Exception):
    """Invalid arguments: exit 2."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # raise instead of exiting so main() owns every exit code
        raise UsageError(message)


def parse_month(text: str, as_of: date) -> date:
    try:
        return service.parse_month(text, as_of)
    except service.MonthError as exc:
        raise UsageError(str(exc)) from None


def _as_of(args, fixture: FixtureCostExplorer | None) -> date:
    today = datetime.now(UTC).date()
    if args.as_of is None:
        return fixture.as_of if fixture else today
    try:
        value = date.fromisoformat(args.as_of)
    except ValueError:
        raise UsageError(f"--as-of must be YYYY-MM-DD, got {args.as_of!r}") from None
    if fixture is None and value > today:
        raise UsageError("--as-of cannot be in the future in live mode")
    return value


def _live_client(profile: str | None):
    import boto3  # live mode only: fixture mode never imports boto3

    from cloudguard.cost.ce import make_client
    return make_client(boto3.Session(profile_name=profile))


def _source(args, fixture: FixtureCostExplorer | None) -> str:
    if fixture is not None:
        return f"fixture {fixture.name} (synthetic data, no AWS call)"
    return f"live AWS Cost Explorer ({CE_REGION}, read-only)"


def _period_arg(args, as_of: date):
    return month_period(parse_month(args.month or args.period, as_of), as_of)


_EXIT = {service.Outcome.ANSWERED: EXIT_OK, service.Outcome.FINDINGS: EXIT_FINDINGS,
         service.Outcome.UNAVAILABLE: EXIT_UNAVAILABLE}


def _cmd_bill(args, client, as_of, now, source):
    period = _period_arg(args, as_of)
    report = service.month_costs(client, period, now, args.max_pages)
    if args.command == "top-services":
        speech = top_service_speech(report, period)
        text = render_bill(report, period, source, focus_services=True)
        extra = {"ranking": service.ranking(report).model_dump(mode="json")}
    else:
        speech = service.month_speech(report, period)
        text = render_bill(report, period, source)
        extra = {}
    return {"period": period.model_dump(mode="json"), **extra, "report": report.model_dump(mode="json")},         speech, text, _EXIT[service.bill_outcome(report.status)]


def _cmd_compare(args, client, as_of, now, source):
    try:
        earlier, later = service.comparison_months(*(args.months or (None, None)), as_of)
    except service.MonthError as exc:
        raise UsageError(str(exc)) from None
    report = service.compare(client, earlier, later, as_of, now, args.max_pages)
    explain = args.command == "explain-increase"
    speech = service.compare_speech(report, explain=explain)
    return {"report": report.model_dump(mode="json")}, speech, render_comparison(report, source, explain=explain),         _EXIT[service.bill_outcome(report.status)]


def _cmd_anomalies(args, client, as_of, now, source):
    if not 1 <= args.lookback_days <= MAX_LOOKBACK_DAYS:
        raise UsageError(f"--lookback-days must be between 1 and {MAX_LOOKBACK_DAYS}")
    report = service.anomalies(client, as_of, args.lookback_days, now, args.max_pages)
    return {"report": report.model_dump(mode="json")}, anomalies_speech(report), render_anomalies(report, source),         _EXIT[service.anomaly_outcome(report.status)]


COMMANDS = {"bill": _cmd_bill, "top-services": _cmd_bill, "compare": _cmd_compare,
            "explain-increase": _cmd_compare, "anomalies": _cmd_anomalies}


def build_parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    common.add_argument("--mode", choices=["fixture", "live"], default="fixture")
    common.add_argument("--as-of", default=None, help="Reference date YYYY-MM-DD (UTC). Default: today in live "
                                                      "mode, the fixture's as_of in fixture mode")
    common.add_argument("--format", choices=["summary", "json", "speech"], default="summary")
    common.add_argument("--fixture", default=None, help="Fixture mode: scenario name or path (default: demo)")
    common.add_argument("--profile", default=None, help="Live mode: AWS profile")
    common.add_argument("--max-pages", type=int, default=None,
                        help=f"Cost Explorer page cap, 1-{MAX_PAGES_LIMIT} (default: $CLOUDGUARD_CE_MAX_PAGES or 5)")

    parser = _Parser(prog="cloudguard.cost", description="Read-only AWS cost intelligence.",
                     epilog="Exit codes: 0 ok, 1 anomalies found, 2 invalid arguments, 3 AWS/API error or data "
                            "unavailable, 4 internal error.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name, help_text in (("bill", "Completed-month bill, or month-to-date spending for the current month"),
                            ("top-services", "Which services cost the most (tax excluded from the ranking)")):
        p = sub.add_parser(name, parents=[common], help=help_text)
        group = p.add_mutually_exclusive_group()
        group.add_argument("--period", choices=["last-month", "this-month"], default="last-month")
        group.add_argument("--month", default=None, help="YYYY-MM, a month name, last-month or this-month")
    for name, help_text in (("compare", "Compare two months (matching days when the later month is current)"),
                            ("explain-increase", "Where a cost increase appears, by service and charge type")):
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("--months", nargs=2, metavar=("EARLIER", "LATER"), default=None,
                       help="Default: the last two completed months")
    a = sub.add_parser("anomalies", parents=[common], help="Unusual charges from AWS Cost Anomaly Detection")
    a.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    return parser


def _emit(args, payload: dict, speech: str, text: str, code: int, as_of: date, fixture) -> None:
    if args.format == "speech":
        print(speech)
    elif args.format == "summary":
        print(text)
    else:
        envelope = {"schema_version": SCHEMA_VERSION, "command": args.command, "mode": args.mode,
                    "as_of": as_of.isoformat(), "fixture": fixture.name if fixture else None,
                    "exit_code": code, "speech": speech, **payload}
        print(json.dumps(envelope, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.max_pages is not None:
            resolve_max_pages(args.max_pages)
        if args.mode == "live" and args.fixture:
            raise UsageError("--fixture applies to fixture mode only")
        if args.mode == "fixture" and args.profile:
            raise UsageError("--profile applies to live mode only")
    except (UsageError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SystemExit as exc:  # --help
        return exc.code if isinstance(exc.code, int) else EXIT_OK

    try:
        fixture = None
        if args.mode == "fixture":
            try:
                resolve_fixture(args.fixture)
            except FixtureError as exc:
                raise UsageError(str(exc)) from None
            fixture = FixtureCostExplorer.load(args.fixture)
        as_of = _as_of(args, fixture)
        client = fixture if fixture is not None else _live_client(args.profile)
        now = datetime.now(UTC)
        payload, speech, text, code = COMMANDS[args.command](args, client, as_of, now, _source(args, fixture))
    except (UsageError, ValueError) as exc:  # ValueError: invalid period or page-cap setting
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except BotoCoreError as exc:  # live session setup, e.g. an unknown profile; no secrets in these messages
        print(f"error: AWS client setup failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except FixtureError as exc:
        print(f"internal error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    except Exception as exc:  # noqa: BLE001 - stable exit code for anything unexpected
        print(f"internal error: {type(exc).__name__}", file=sys.stderr)
        return EXIT_INTERNAL
    _emit(args, payload, speech, text, code, as_of, fixture)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
