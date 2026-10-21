"""Cost intelligence MCP tools, through a real MCP client session over the SDK's in-memory transport.

Fixture contexts use synthetic data; live contexts use a botocore Stubber client. Nothing calls AWS.
"""

import json
import re
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import anyio
import jsonschema
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from cloudguard.cost.fixture import FixtureCostExplorer
from cloudguard.server.app import create_server
from cloudguard.server.context import CostContext
from cloudguard.server.cost_tools import COST_TOOL_NAMES, MAX_RANKING

BACKEND = Path(__file__).resolve().parents[1]
READ_OPERATIONS = {"get_cost_and_usage", "get_anomaly_monitors", "get_anomalies"}
TOP_LEVEL = {"schema_version", "tool", "outcome", "mode", "data_source", "as_of", "currency", "freshness", "speech",
             "report"}


def session_run(ctx, fn):
    """Run `fn(client_session)` against a fresh server for `ctx`."""
    async def main():
        async with create_connected_server_and_client_session(create_server(ctx)) as client:
            return await fn(client)
    return anyio.run(main)


def call(ctx, name, args=None):
    async def fn(client):
        return await client.call_tool(name, args or {})
    return session_run(ctx, fn)


def ok(ctx, name, args=None):
    result = call(ctx, name, args)
    assert not result.isError, result.content
    return result.structuredContent


def error_text(ctx, name, args=None):
    result = call(ctx, name, args)
    assert result.isError and result.structuredContent is None
    return result.content[0].text


def fixture(name="demo", **kwargs):
    return CostContext.fixture(name, **kwargs)


def list_tools(ctx):
    async def fn(client):
        return (await client.list_tools()).tools
    return session_run(ctx, fn)


# ---------- registration and discovery ----------

def test_exactly_the_six_cost_tools_are_registered():
    tools = list_tools(fixture())
    assert sorted(t.name for t in tools) == sorted(COST_TOOL_NAMES)


def test_tools_are_annotated_read_only_with_strict_schemas():
    for tool in list_tools(fixture()):
        assert tool.description and len(tool.description) > 80
        a = tool.annotations
        assert a.readOnlyHint is True and a.destructiveHint is False and a.idempotentHint is True
        assert a.openWorldHint is False  # fixture mode never leaves the process
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.outputSchema and tool.outputSchema["type"] == "object"
        assert TOP_LEVEL <= set(tool.outputSchema["properties"])


def test_no_tool_accepts_account_role_profile_region_or_credentials():
    forbidden = re.compile(r"account|role|arn|profile|region|credential|key|secret|token|session", re.I)
    for tool in list_tools(fixture()):
        for param in tool.inputSchema.get("properties", {}):
            assert not forbidden.search(param), (tool.name, param)


def test_live_context_marks_tools_open_world(monkeypatch):
    ctx = CostContext(mode="live", client=object(), data_source="test", as_of=lambda: date(2026, 10, 16))
    assert all(t.annotations.openWorldHint for t in list_tools(ctx))


# ---------- input validation ----------

@pytest.mark.parametrize("name,args,needle", [
    ("get_monthly_aws_bill", {"month": "2026-13"}, "invalid month"),
    ("get_monthly_aws_bill", {"month": "2026-11"}, "in the future"),
    ("get_monthly_aws_bill", {"month": "this-month"}, "get_current_month_spending"),
    ("get_monthly_aws_bill", {"month": "2026-10"}, "still in progress"),
    ("get_monthly_aws_bill", {"month": "Sept; DROP"}, "validation error"),
    ("get_monthly_aws_bill", {"month": "x" * 50}, "validation error"),
    ("get_monthly_aws_bill", {"month": 202609}, "validation error"),
    ("get_cost_breakdown", {"limit": 0}, "validation error"),
    ("get_cost_breakdown", {"limit": MAX_RANKING + 1}, "validation error"),
    ("compare_monthly_costs", {"earlier_month": "2026-10", "later_month": "2026-09"}, "earlier month"),
    ("compare_monthly_costs", {"earlier_month": "2026-09"}, "both months"),
    ("investigate_cost_increase", {"later_month": "2026-09"}, "both months"),
    ("get_cost_anomalies", {"lookback_days": 0}, "validation error"),
    ("get_cost_anomalies", {"lookback_days": 91}, "validation error"),
])
def test_invalid_input_is_a_tool_error(name, args, needle):
    assert needle in error_text(fixture(), name, args)


@pytest.mark.parametrize("arg", ["account_id", "role_arn", "profile", "region", "aws_access_key_id", "as_of"])
def test_unknown_arguments_are_rejected_not_ignored(arg):
    for name in COST_TOOL_NAMES:
        assert "does not accept" in error_text(fixture(), name, {arg: "x"})


# ---------- the six questions ----------

def test_monthly_bill_last_month():
    r = ok(fixture(), "get_monthly_aws_bill")
    assert r["outcome"] == "answered" and r["is_final_bill"] is True and r["currency"] == "USD"
    assert r["period"]["kind"] == "completed_month" and r["period"]["start"] == "2026-09-01"
    m = r["report"]["month"]
    for field, value in [("charges_before_discounts_credits_and_refunds", "45.08"), ("tax", "6.98"),
                         ("fees", "2.90"), ("credits", "-2.00"), ("refunds", "-0.25"), ("calculated_net", "42.83")]:
        assert Decimal(m[field]) == Decimal(value)
    assert r["report"]["comparison"]["previous_start"] == "2026-08-01"
    assert r["speech"].startswith("For the September 2026 AWS billing month")
    assert r["freshness"]["aws_requests"] == 1 and r["freshness"]["retrieved_at"]


@pytest.mark.parametrize("month", ["2026-08", "august", "Aug"])
def test_monthly_bill_historical_month(month):
    r = ok(fixture(), "get_monthly_aws_bill", {"month": month})
    assert r["period"]["start"] == "2026-08-01"
    assert Decimal(r["report"]["month"]["charges_before_discounts_credits_and_refunds"]) == Decimal("25.59")


def test_month_without_data_is_unavailable_not_zero():
    r = ok(fixture(), "get_monthly_aws_bill", {"month": "2025-01"})
    assert r["outcome"] == "unavailable" and r["report"]["status"] == "NO_DATA" and r["report"]["month"] is None
    assert "no cost data" in r["speech"]


def test_current_month_spending_is_month_to_date():
    r = ok(fixture(), "get_current_month_spending")
    assert r["is_final_bill"] is False and r["period"]["kind"] == "month_to_date"
    assert (r["period"]["start"], r["period"]["end_exclusive"]) == ("2026-10-01", "2026-10-17")
    assert r["freshness"]["estimated"] is True and r["report"]["comparison"] is None
    assert "not a final bill" in r["speech"] and "billing month" not in r["speech"]


def test_cost_breakdown_ranks_services_and_discloses_tax():
    r = ok(fixture(), "get_cost_breakdown", {"limit": 3})
    ranking = r["ranking"]
    assert [s["rank"] for s in ranking["services"]] == [1, 2, 3]
    assert ranking["services"][0]["service"] == "Amazon Elastic Compute Cloud - Compute"
    assert "Tax" not in [s["service"] for s in ranking["services"]]
    assert Decimal(ranking["excluded_tax"]) == Decimal("6.98")
    assert ranking["total_services"] == 5 and ranking["truncated"] is True
    assert len(r["report"]["month"]["services"]) == 6  # full evidence keeps every service, tax included
    assert "tax" not in r["speech"].lower() and r["is_final_bill"] is True


def test_cost_breakdown_current_month_is_labelled():
    r = ok(fixture(), "get_cost_breakdown", {"month": "this-month"})
    assert r["is_final_bill"] is False and r["period"]["kind"] == "month_to_date"
    assert r["speech"].startswith("So far in October 2026")


def test_compare_completed_months_default():
    r = ok(fixture(), "compare_monthly_costs")
    rep = r["report"]
    assert rep["basis"] == "full_months"
    assert (rep["earlier"]["start"], rep["later"]["start"]) == ("2026-08-01", "2026-09-01")
    assert Decimal(rep["charges_change"]) == Decimal("19.49")


def test_compare_completed_with_in_progress_month_uses_matching_days():
    r = ok(fixture(), "compare_monthly_costs", {"earlier_month": "september", "later_month": "october"})
    rep = r["report"]
    assert rep["basis"] == "matching_days" and rep["later"]["kind"] == "month_to_date"
    assert rep["earlier"]["kind"] == "partial_month" and rep["earlier"]["days"] == rep["later"]["days"] == 16
    assert Decimal(rep["earlier_full_month_cost"]["charges_before_discounts_credits_and_refunds"]) == Decimal("45.08")
    assert "MONTH_TO_DATE" in rep["warnings"] and r["freshness"]["aws_requests"] == 2


def test_investigate_increase_names_location_never_cause():
    r = ok(fixture(), "investigate_cost_increase")
    x = r["report"]["explanation"]
    assert r["tool"] == "investigate_cost_increase" and x["verdict"] == "INCREASE_ATTRIBUTED"
    assert x["drivers"][0]["name"] == "Amazon Elastic Compute Cloud - Compute"
    assert sum(Decimal(d["change"]) for d in x["drivers"] + x["offsets"]) == Decimal(x["net_change"])
    assert "not why" in r["speech"] and "because" not in r["speech"]


def test_investigate_increase_cannot_determine_with_unclassified():
    r = ok(fixture("unclassified"), "investigate_cost_increase")
    assert r["report"]["explanation"]["verdict"] == "CANNOT_DETERMINE"
    assert "can't determine" in r["speech"]


def test_investigate_when_costs_fell():
    r = ok(fixture("decrease"), "investigate_cost_increase")
    assert r["report"]["explanation"]["verdict"] == "NO_INCREASE"


@pytest.mark.parametrize("scenario,status,outcome,stale", [
    ("demo", "ANOMALIES_FOUND", "findings", False),
    ("decrease", "NO_ANOMALIES", "answered", False),
    ("stale-anomalies", "ANOMALIES_FOUND", "findings", True),
    ("no-monitor", "NO_MONITOR", "unavailable", False),
    ("anomalies-not-evaluated", "NOT_YET_EVALUATED", "unavailable", False),
    ("access-denied", "ACCESS_DENIED", "unavailable", False),
    ("data-unavailable", "ERROR", "unavailable", False),
])
def test_anomaly_states_are_distinguished(scenario, status, outcome, stale):
    r = ok(fixture(scenario), "get_cost_anomalies")
    assert r["report"]["status"] == status and r["outcome"] == outcome
    assert r["freshness"]["anomaly_data_stale"] is stale
    if status != "ANOMALIES_FOUND":
        assert r["report"]["anomalies"] == []


def test_anomaly_lookback_window():
    r = ok(fixture(), "get_cost_anomalies", {"lookback_days": 2})
    assert (r["report"]["window_start"], r["report"]["window_end"]) == ("2026-10-14", "2026-10-16")


# ---------- pagination and AWS errors ----------

def test_paginated_results_match_single_page():
    single = ok(fixture(), "get_monthly_aws_bill")
    paged = ok(fixture("paginated"), "get_monthly_aws_bill")
    assert paged["freshness"]["aws_requests"] > 1 and paged["report"]["month"] == single["report"]["month"]


@pytest.mark.parametrize("name", ["get_monthly_aws_bill", "get_current_month_spending", "get_cost_breakdown",
                                  "compare_monthly_costs", "investigate_cost_increase"])
def test_page_failure_returns_no_partial_totals(name):
    r = ok(fixture("pagination-error"), name)
    rep = r["report"]
    assert r["outcome"] == "unavailable" and rep["status"] == "ERROR"
    assert rep["error"]["code"] == "ThrottlingException"
    assert rep.get("month") is None and rep.get("later_month") is None and rep.get("net_change") is None


def test_page_cap_from_server_context_refuses_partial_totals():
    r = ok(fixture("paginated", max_pages=2), "get_monthly_aws_bill")
    assert r["outcome"] == "unavailable" and r["report"]["error"]["code"] == "PageLimitExceeded"
    assert r["report"]["month"] is None


@pytest.mark.parametrize("scenario,status", [("access-denied", "ACCESS_DENIED"),
                                             ("data-unavailable", "DATA_UNAVAILABLE")])
def test_cost_api_errors_from_fixture(scenario, status):
    r = ok(fixture(scenario), "get_monthly_aws_bill")
    assert r["outcome"] == "unavailable" and r["report"]["status"] == status and r["currency"] is None


GROUP_BY = [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "RECORD_TYPE"}]


def _live(monkeypatch=None):
    import boto3
    from botocore.stub import Stubber

    from cloudguard.cost.ce import CE_CONFIG, CE_REGION
    client = boto3.client("ce", region_name=CE_REGION, config=CE_CONFIG, aws_access_key_id="testing",
                          aws_secret_access_key="testing")
    ctx = CostContext(mode="live", client=client, data_source="live AWS Cost Explorer (stubbed)",
                      as_of=lambda: date(2026, 10, 16))
    return ctx, Stubber(client)


def _params(start, end, token=None):
    p = {"TimePeriod": {"Start": start, "End": end}, "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"],
         "GroupBy": GROUP_BY}
    return p | ({"NextPageToken": token} if token else {})


def _r(start, end, groups, estimated=False):
    return {"TimePeriod": {"Start": start, "End": end}, "Total": {}, "Groups": groups, "Estimated": estimated}


def _g(service, record_type, amount):
    return {"Keys": [service, record_type], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}


def test_live_context_paginates_with_stubbed_cost_explorer():
    ctx, stub = _live()
    stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _r("2026-08-01", "2026-09-01", [_g("AWS Lambda", "Usage", "1.00")]),
        _r("2026-09-01", "2026-10-01", [_g("AWS Lambda", "Usage", "2.00")])], "NextPageToken": "p2"},
        _params("2026-08-01", "2026-10-01"))
    stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _r("2026-09-01", "2026-10-01", [_g("Tax", "Tax", "0.36")])]}, _params("2026-08-01", "2026-10-01", "p2"))
    with stub:
        r = ok(ctx, "get_monthly_aws_bill")
        stub.assert_no_pending_responses()
    assert r["mode"] == "live" and r["freshness"]["aws_requests"] == 2
    assert Decimal(r["report"]["month"]["charges_before_discounts_credits_and_refunds"]) == Decimal("2.36")


@pytest.mark.parametrize("code,http,status", [("AccessDeniedException", 400, "ACCESS_DENIED"),
                                              ("DataUnavailableException", 400, "DATA_UNAVAILABLE"),
                                              ("ServiceUnavailableException", 503, "ERROR")])
def test_live_service_errors(code, http, status):
    ctx, stub = _live()
    stub.add_client_error("get_cost_and_usage", code, "User: arn:aws:iam::123456789012:user/x is not authorized",
                          http, expected_params=_params("2026-10-01", "2026-10-17"))
    with stub:
        r = ok(ctx, "get_current_month_spending")
    assert r["outcome"] == "unavailable" and r["report"]["status"] == status
    assert "123456789012" not in json.dumps(r) and "arn:" not in json.dumps(r)


def test_live_anomaly_access_denied():
    ctx, stub = _live()
    stub.add_client_error("get_anomaly_monitors", "AccessDeniedException", "denied", 400)
    with stub:
        r = ok(ctx, "get_cost_anomalies")
    assert r["outcome"] == "unavailable" and r["report"]["status"] == "ACCESS_DENIED"


# ---------- account context, authorization boundary, no writes ----------

class Recorder:
    """Wraps a client and records every attribute the tools touch."""

    def __init__(self, inner):
        self._inner, self.touched = inner, []

    def __getattr__(self, name):
        self.touched.append(name)
        return getattr(self._inner, name)


def _recorded(name="demo"):
    rec = Recorder(FixtureCostExplorer.load(name))
    ctx = CostContext(mode="fixture", client=rec, data_source="recorded fixture", as_of=lambda: date(2026, 10, 16))
    return ctx, rec


def test_every_tool_uses_only_read_operations_on_the_startup_client():
    ctx, rec = _recorded()
    for name in COST_TOOL_NAMES:
        ok(ctx, name)
    ok(ctx, "compare_monthly_costs", {"earlier_month": "september", "later_month": "october"})
    assert rec.touched and set(rec.touched) <= READ_OPERATIONS


def test_rejected_context_arguments_never_reach_aws():
    ctx, rec = _recorded()
    for name in COST_TOOL_NAMES:
        error_text(ctx, name, {"account_id": "000000000000", "role_arn": "arn:aws:iam::000000000000:role/x"})
    assert rec.touched == []


def test_two_servers_keep_their_own_context():
    a, b = fixture("demo"), fixture("decrease")
    assert ok(a, "investigate_cost_increase")["report"]["explanation"]["verdict"] == "INCREASE_ATTRIBUTED"
    assert ok(b, "investigate_cost_increase")["report"]["explanation"]["verdict"] == "NO_INCREASE"
    assert ok(a, "get_monthly_aws_bill")["data_source"].startswith("fixture demo.json")


def test_no_write_api_in_cost_or_server_code():
    write = re.compile(r"\.(create|put|update|delete|tag|untag|start|stop|provide)_[a-z_]+\(")
    for package in ("cost", "server"):
        for path in (BACKEND / "cloudguard" / package).glob("*.py"):
            assert not write.search(path.read_text(encoding="utf-8")), path.name


def test_results_never_carry_account_identifiers():
    for name in COST_TOOL_NAMES:
        text = json.dumps(ok(fixture(), name))
        assert "000000000000" not in text and "arn:" not in text


# ---------- structured output ----------

def test_structured_content_matches_advertised_output_schema():
    tools = {t.name: t for t in list_tools(fixture())}
    cases = [(n, {}) for n in COST_TOOL_NAMES] + [
        ("compare_monthly_costs", {"earlier_month": "september", "later_month": "october"}),
        ("get_monthly_aws_bill", {"month": "2025-01"})]
    for scenario in ("demo", "pagination-error", "no-monitor", "credits-refunds"):
        for name, args in cases:
            result = call(fixture(scenario), name, args)
            assert not result.isError
            r = result.structuredContent
            jsonschema.validate(r, tools[name].outputSchema)
            assert TOP_LEVEL <= set(r) and r["schema_version"] == "1" and r["tool"] == name
            assert r["mode"] == "fixture" and r["as_of"] == "2026-10-16"
            assert json.loads(result.content[0].text) == r  # the text block mirrors the structured content


def test_net_credit_is_spoken_naturally():
    r = ok(fixture("credits-refunds"), "get_monthly_aws_bill")
    assert Decimal(r["report"]["month"]["calculated_net"]) == Decimal("-0.50")
    assert "a net credit of 0.50" in r["speech"]


# ---------- process-level checks ----------

def test_fixture_server_imports_no_boto3():
    script = ("import sys\nfrom cloudguard.server.app import create_server\n"
              "from cloudguard.server.context import CostContext\n"
              "create_server(CostContext.fixture())\nassert 'boto3' not in sys.modules\nprint('OK')")
    proc = subprocess.run([sys.executable, "-B", "-c", script], cwd=BACKEND, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0 and "OK" in proc.stdout, proc.stderr


@pytest.mark.parametrize("argv", [["--profile", "x"], ["--mode", "live", "--fixture", "demo"],
                                  ["--transport", "sse"], ["--max-pages", "0"]])
def test_server_cli_rejects_bad_options(argv):
    from cloudguard.server.__main__ import main
    with pytest.raises((SystemExit, ValueError)):
        main(argv)


def test_http_transport_binds_to_localhost_and_is_stateless():
    server = create_server(fixture())
    assert server.settings.host == "127.0.0.1"
    assert server.settings.stateless_http is True and server.settings.json_response is True
