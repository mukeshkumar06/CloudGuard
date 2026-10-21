"""Cost Intelligence CLI (python -m cloudguard.cost): fixture scenarios, live mode against botocore Stubber.

No test touches AWS. Live-mode tests replace the client factory with a Stubber-backed client.
"""

import json
import os
import re
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import cloudguard.cost.__main__ as cli
from cloudguard.cost.compare import month_period
from cloudguard.cost.fixture import FIXTURE_DIR, FixtureCostExplorer
from cloudguard.cost.models import PeriodKind

BACKEND = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 10, 16)
SCENARIOS = sorted(p.stem for p in FIXTURE_DIR.glob("*.json"))
REAL_LIVE_CLIENT = cli._live_client


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def run_json(capsys, *argv):
    code, out, err = run(capsys, *argv, "--format", "json")
    assert err == ""
    data = json.loads(out)
    assert data["exit_code"] == code
    return code, data


@pytest.fixture(autouse=True)
def no_live_client(monkeypatch):
    """Fixture-mode tests must never reach the live client factory."""
    def refuse(profile):
        raise AssertionError("live client requested")
    monkeypatch.setattr(cli, "_live_client", refuse)


# ---------- fixtures are synthetic and complete ----------

def test_every_scenario_file_is_synthetic_and_has_no_real_identifiers():
    expected = {"demo", "decrease", "credits-refunds", "savings-plans", "unclassified", "no-monitor",
                "stale-anomalies", "anomalies-not-evaluated", "paginated", "pagination-error", "access-denied",
                "data-unavailable"}
    assert expected <= set(SCENARIOS)
    for name in SCENARIOS:
        text = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
        data = json.loads(text)
        assert data["synthetic"] is True and data["description"]
        for account in re.findall(r"\b\d{12}\b", text):
            assert account == "000000000000"


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("command", ["bill", "top-services", "compare", "explain-increase", "anomalies"])
@pytest.mark.parametrize("fmt", ["summary", "json", "speech"])
def test_every_command_runs_on_every_scenario(capsys, name, command, fmt):
    code, out, err = run(capsys, command, "--fixture", name, "--format", fmt)
    assert code in (0, 1, 3), err
    assert out.strip()
    assert "arn:" not in out and "000000000000" not in out


# ---------- Q1: last month's bill ----------

def test_bill_last_month_completed(capsys):
    code, data = run_json(capsys, "bill")
    assert code == cli.EXIT_OK
    assert data["mode"] == "fixture" and data["fixture"] == "demo.json" and data["as_of"] == "2026-10-16"
    assert data["period"]["kind"] == "completed_month"
    assert (data["period"]["start"], data["period"]["end_exclusive"]) == ("2026-09-01", "2026-10-01")
    report = data["report"]
    assert report["status"] == "OK" and report["currency"] == "USD" and report["metric"] == "UnblendedCost"
    m = report["month"]
    assert Decimal(m["charges_before_discounts_credits_and_refunds"]) == Decimal("45.08")
    assert Decimal(m["tax"]) == Decimal("6.98") and Decimal(m["fees"]) == Decimal("2.90")
    assert Decimal(m["credits"]) == Decimal("-2.00") and Decimal(m["refunds"]) == Decimal("-0.25")
    assert Decimal(m["calculated_net"]) == Decimal("42.83")
    assert report["comparison"]["previous_start"] == "2026-08-01"
    assert report["aws_requests"] == 1


def test_bill_speech_and_summary(capsys):
    _, speech, _ = run(capsys, "bill", "--format", "speech")
    assert speech.startswith("For the September 2026 AWS billing month")
    assert "US dollars" in speech and "Tax" not in speech
    _, text, _ = run(capsys, "bill")
    assert "Completed-month bill" in text and "Currency:  USD" in text
    for disclosed in ("Tax", "Fees:", "Credits:", "Refunds:", "Tax (not ranked as a service)", "Evidence"):
        assert disclosed in text


def test_bill_for_explicit_month_and_month_name(capsys):
    for month in ("2026-08", "august", "Aug"):
        code, data = run_json(capsys, "bill", "--month", month)
        assert code == 0 and data["period"]["start"] == "2026-08-01"
        assert Decimal(data["report"]["month"]["charges_before_discounts_credits_and_refunds"]) == Decimal("25.59")


def test_bill_month_without_data_is_not_zero(capsys):
    code, data = run_json(capsys, "bill", "--month", "2025-01")
    assert code == cli.EXIT_UNAVAILABLE
    assert data["report"]["status"] == "NO_DATA" and data["report"]["month"] is None
    assert "no cost data" in data["speech"]


# ---------- Q2: this month so far ----------

def test_this_month_is_month_to_date_never_a_bill(capsys):
    code, data = run_json(capsys, "bill", "--period", "this-month")
    assert code == 0
    assert data["period"]["kind"] == "month_to_date"
    assert (data["period"]["start"], data["period"]["end_exclusive"]) == ("2026-10-01", "2026-10-17")
    assert data["report"]["comparison"] is None
    assert data["report"]["month"]["estimated"] is True
    speech = data["speech"]
    assert speech.startswith("So far in October 2026, from October 1 to October 16")
    assert "not a final bill" in speech and "billing month" not in speech
    _, text, _ = run(capsys, "bill", "--period", "this-month")
    assert "Month-to-date spending - NOT a final bill" in text and "Completed-month bill" not in text


def test_explicit_current_month_is_also_month_to_date(capsys):
    _, data = run_json(capsys, "bill", "--month", "2026-10")
    assert data["period"]["kind"] == "month_to_date"


# ---------- Q3: compare ----------

def test_compare_defaults_to_last_two_completed_months(capsys):
    code, data = run_json(capsys, "compare")
    r = data["report"]
    assert code == 0 and r["basis"] == "full_months"
    assert (r["earlier"]["start"], r["later"]["start"]) == ("2026-08-01", "2026-09-01")
    assert Decimal(r["charges_change"]) == Decimal("19.49") and Decimal(r["charges_change_percent"]) == Decimal("76.2")
    assert "19.49 more, up 76.2 percent" in data["speech"]


def test_compare_september_october_uses_matching_days(capsys):
    code, data = run_json(capsys, "compare", "--months", "september", "october")
    r = data["report"]
    assert code == 0 and r["basis"] == "matching_days"
    assert r["later"]["kind"] == "month_to_date" and r["earlier"]["kind"] == "partial_month"
    assert (r["earlier"]["end_exclusive"], r["earlier"]["days"], r["later"]["days"]) == ("2026-09-17", 16, 16)
    assert r["earlier_full_month"]["end_exclusive"] == "2026-10-01"
    assert Decimal(r["earlier_full_month_cost"]["charges_before_discounts_credits_and_refunds"]) == Decimal("45.08")
    assert Decimal(r["earlier_month"]["charges_before_discounts_credits_and_refunds"]) == Decimal("24.74")
    assert r["aws_requests"] == 2
    assert "MONTH_TO_DATE" in r["warnings"]
    assert "still in progress" in data["speech"] and "first 16 days" in data["speech"]


def test_compare_rejects_reversed_or_future_months(capsys):
    assert run(capsys, "compare", "--months", "2026-10", "2026-09")[0] == cli.EXIT_USAGE
    assert run(capsys, "compare", "--months", "2026-09", "2026-11")[0] == cli.EXIT_USAGE


def test_comparison_net_change_splits_exactly_into_drivers(capsys):
    for name in SCENARIOS:
        code, data = run_json(capsys, "explain-increase", "--fixture", name)
        r = data["report"]
        if r["status"] != "OK":
            continue
        x = r["explanation"]
        parts = [Decimal(d["change"]) for d in x["drivers"] + x["offsets"]] + [Decimal(x["below_threshold_change"])]
        assert sum(parts, Decimal(0)) == Decimal(x["net_change"]) == Decimal(r["net_change"])
        assert sum(Decimal(s["change"]) for s in r["service_changes"]) == Decimal(r["charges_change"])


# ---------- Q4: top service ----------

def test_top_services_excludes_tax_from_ranking_but_discloses_it(capsys):
    code, data = run_json(capsys, "top-services")
    assert code == 0
    ranking = data["ranking"]
    assert [s["service"] for s in ranking["services"]][:2] == ["Amazon Elastic Compute Cloud - Compute",
                                                               "AWS Support (Developer)"]
    assert "Tax" not in [s["service"] for s in ranking["services"]]
    assert Decimal(ranking["excluded_tax"]) == Decimal("6.98")
    assert "highest-cost AWS service was Amazon Elastic Compute Cloud - Compute at 31.40 US dollars" in data["speech"]
    assert "tax" not in data["speech"].lower()
    _, text, _ = run(capsys, "top-services")
    assert "Top AWS services by cost" in text and "Tax (not ranked as a service)" in text


def test_top_services_this_month_says_so_far(capsys):
    _, speech, _ = run(capsys, "top-services", "--period", "this-month", "--format", "speech")
    assert speech.startswith("So far in October 2026") and "not a final bill" in speech


# ---------- Q5: why did costs increase ----------

def test_explain_increase_locates_change_without_inventing_a_cause(capsys):
    code, data = run_json(capsys, "explain-increase")
    x = data["report"]["explanation"]
    assert code == 0 and x["verdict"] == "INCREASE_ATTRIBUTED"
    assert x["drivers"][0]["name"] == "Amazon Elastic Compute Cloud - Compute"
    assert Decimal(x["drivers"][0]["change"]) == Decimal("13.20")
    assert "not why" in data["speech"] and "can't say what caused it" in data["speech"]
    for word in ("because", "due to", "caused by"):
        assert word not in data["speech"]


def test_explain_increase_unclassified_cannot_determine(capsys):
    code, data = run_json(capsys, "explain-increase", "--fixture", "unclassified")
    x = data["report"]["explanation"]
    assert code == 0 and x["verdict"] == "CANNOT_DETERMINE" and x["reasons"]
    assert "can't determine" in data["speech"]


def test_explain_increase_when_costs_fell(capsys):
    _, data = run_json(capsys, "explain-increase", "--fixture", "decrease")
    assert data["report"]["explanation"]["verdict"] == "NO_INCREASE"
    assert data["speech"].startswith("Your costs didn't increase")


def test_explain_increase_without_earlier_data_cannot_determine(capsys):
    code, data = run_json(capsys, "explain-increase", "--months", "2025-01", "2026-09")
    assert code == cli.EXIT_UNAVAILABLE and data["report"]["status"] == "NO_DATA"
    assert data["speech"].startswith("I can't determine why your costs changed")
    assert "January 2025" in data["speech"]


# ---------- Q6: unusual charges ----------

def test_anomalies_found_exit_code_one(capsys):
    code, data = run_json(capsys, "anomalies")
    r = data["report"]
    assert code == cli.EXIT_FINDINGS and r["status"] == "ANOMALIES_FOUND"
    assert (r["window_start"], r["window_end"]) == ("2026-10-09", "2026-10-16")
    assert r["currency"] == "USD" and r["anomalies"][0]["end_date"] is None
    assert "1 anomaly" in data["speech"]


def test_no_anomalies_exit_zero_with_caveat(capsys):
    code, data = run_json(capsys, "anomalies", "--fixture", "decrease")
    assert code == 0 and data["report"]["status"] == "NO_ANOMALIES"
    assert "doesn't guarantee" in data["speech"]


@pytest.mark.parametrize("name,status", [("no-monitor", "NO_MONITOR"),
                                         ("anomalies-not-evaluated", "NOT_YET_EVALUATED"),
                                         ("access-denied", "ACCESS_DENIED"), ("data-unavailable", "ERROR"),
                                         ("pagination-error", "ERROR")])
def test_anomaly_data_missing_or_failing_exit_three(capsys, name, status):
    code, data = run_json(capsys, "anomalies", "--fixture", name)
    assert code == cli.EXIT_UNAVAILABLE and data["report"]["status"] == status
    assert data["report"]["anomalies"] == []


def test_stale_anomaly_data_is_flagged(capsys):
    code, data = run_json(capsys, "anomalies", "--fixture", "stale-anomalies")
    assert code == cli.EXIT_FINDINGS
    assert data["report"]["stale"] is True and "STALE_EVALUATION" in data["report"]["warnings"]
    assert "more than two days ago" in data["speech"]
    _, text, _ = run(capsys, "anomalies", "--fixture", "stale-anomalies")
    assert "[STALE]" in text


def test_anomalies_lookback_validation(capsys):
    assert run(capsys, "anomalies", "--lookback-days", "0")[0] == cli.EXIT_USAGE
    assert run(capsys, "anomalies", "--lookback-days", "91")[0] == cli.EXIT_USAGE
    code, data = run_json(capsys, "anomalies", "--lookback-days", "2")
    assert data["report"]["window_start"] == "2026-10-14"


def test_anomalies_generated_at_is_real_time_not_pinned(capsys):
    _, data = run_json(capsys, "anomalies", "--as-of", "2026-10-16")
    assert not data["report"]["generated_at"].startswith("2026-10-16T12:00:00")


# ---------- credits, refunds, Savings Plans, unclassified ----------

def test_credits_and_refunds_net_credit_spoken_naturally(capsys):
    _, data = run_json(capsys, "bill", "--fixture", "credits-refunds")
    m = data["report"]["month"]
    assert Decimal(m["calculated_net"]) == Decimal("-0.50")
    assert "a net credit of 0.50" in data["speech"] and "-0.50" not in data["speech"]
    _, text, _ = run(capsys, "bill", "--fixture", "credits-refunds")
    assert "(net credit)" in text and "Credits:" in text and "Refunds:" in text


def test_savings_plan_negation_and_fee(capsys):
    _, data = run_json(capsys, "bill", "--fixture", "savings-plans")
    m = data["report"]["month"]
    assert Decimal(m["usage"]) == Decimal("23.00") and Decimal(m["savings_plan_negation"]) == Decimal("-20.00")
    assert Decimal(m["fees"]) == Decimal("12.00")
    _, text, _ = run(capsys, "bill", "--fixture", "savings-plans")
    assert "Savings Plan negation: offsets usage" in text


def test_unclassified_record_types_reported_not_charged(capsys):
    _, data = run_json(capsys, "bill", "--fixture", "unclassified")
    m = data["report"]["month"]
    assert set(m["unclassified_record_types"]) == {"Marketplace adjustment", "Synthetic future type"}
    assert Decimal(m["unclassified"]) == Decimal("5.85")
    assert "UNCLASSIFIED_RECORD_TYPES" in data["report"]["warnings"]
    assert "could not be classified" in data["speech"]


# ---------- pagination and API errors ----------

def test_paginated_fixture_matches_single_page_totals(capsys):
    _, single = run_json(capsys, "bill")
    _, paged = run_json(capsys, "bill", "--fixture", "paginated")
    assert paged["report"]["aws_requests"] > 1
    assert paged["report"]["month"] == single["report"]["month"]
    _, anomalies = run_json(capsys, "anomalies", "--fixture", "paginated")
    assert anomalies["report"]["monitor_count"] == 2 and len(anomalies["report"]["anomalies"]) == 2
    assert anomalies["report"]["aws_requests"] == 4


def test_page_cap_refuses_partial_totals(capsys):
    code, data = run_json(capsys, "bill", "--fixture", "paginated", "--max-pages", "2")
    assert code == cli.EXIT_UNAVAILABLE
    assert data["report"]["status"] == "ERROR" and data["report"]["month"] is None
    assert data["report"]["error"]["code"] == "PageLimitExceeded"


def test_failure_on_second_page_reports_no_partial_totals(capsys):
    for command in ("bill", "compare", "explain-increase", "top-services"):
        code, data = run_json(capsys, command, "--fixture", "pagination-error")
        r = data["report"]
        assert code == cli.EXIT_UNAVAILABLE and r["status"] == "ERROR"
        assert r["error"]["code"] == "ThrottlingException" and r["aws_requests"] == 2
        assert r.get("month") is None and r.get("later_month") is None and r.get("net_change") is None


@pytest.mark.parametrize("name,status", [("access-denied", "ACCESS_DENIED"), ("data-unavailable", "DATA_UNAVAILABLE")])
def test_cost_api_errors(capsys, name, status):
    code, data = run_json(capsys, "bill", "--fixture", name)
    assert code == cli.EXIT_UNAVAILABLE and data["report"]["status"] == status
    assert "synthetic fixture failure" not in json.dumps(data)  # AWS's raw message is never passed through
    _, text, _ = run(capsys, "bill", "--fixture", name)
    assert f"AWS error: {data['report']['error']['code']}" in text


# ---------- arguments, dates and exit codes ----------

@pytest.mark.parametrize("text,expected", [
    ("2026-09", date(2026, 9, 1)), ("september", date(2026, 9, 1)), ("Sep", date(2026, 9, 1)),
    ("november", date(2025, 11, 1)), ("this-month", date(2026, 10, 1)), ("last-month", date(2026, 9, 1)),
])
def test_parse_month(text, expected):
    assert cli.parse_month(text, AS_OF) == expected


@pytest.mark.parametrize("text", ["2026-13", "2026-11", "smarch", "se", ""])
def test_parse_month_rejects(text):
    with pytest.raises(cli.UsageError):
        cli.parse_month(text, AS_OF)


def test_last_month_across_year_boundary():
    assert cli.parse_month("last-month", date(2026, 1, 5)) == date(2025, 12, 1)


def test_month_period_kinds():
    mtd = month_period(date(2026, 10, 1), AS_OF)
    assert mtd.kind == PeriodKind.MONTH_TO_DATE and mtd.end_exclusive == date(2026, 10, 17) and mtd.days == 16
    done = month_period(date(2026, 2, 1), AS_OF)
    assert done.kind == PeriodKind.COMPLETED_MONTH and done.end_exclusive == date(2026, 3, 1) and done.days == 28
    first = month_period(date(2026, 10, 1), date(2026, 10, 1))
    assert first.end_exclusive == date(2026, 10, 2)  # never an empty Start == End request


def test_as_of_pins_the_reference_date(capsys):
    _, data = run_json(capsys, "bill", "--as-of", "2026-09-20")
    assert data["as_of"] == "2026-09-20" and data["period"]["start"] == "2026-08-01"


@pytest.mark.parametrize("argv", [
    ["bill", "--as-of", "16-10-2026"], ["bill", "--month", "2026-11"], ["bill", "--format", "xml"],
    ["bill", "--fixture", "no-such-scenario"], ["bill", "--max-pages", "21"], ["bill", "--profile", "x"],
    ["bill", "--period", "this-month", "--month", "2026-09"], ["frobnicate"], [],
    ["bill", "--mode", "live", "--fixture", "demo"], ["compare", "--months", "2026-09"],
])
def test_invalid_arguments_exit_two(capsys, argv):
    code, out, err = run(capsys, *argv)
    assert code == cli.EXIT_USAGE and out == "" and err.startswith("error:")


def test_help_exits_zero(capsys):
    assert run(capsys, "--help")[0] == 0


def test_malformed_fixture_is_internal_error(capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"synthetic": true}', encoding="utf-8")  # no as_of
    code, _, err = run(capsys, "bill", "--fixture", str(bad))
    assert code == cli.EXIT_INTERNAL and "internal error" in err


def test_non_synthetic_fixture_refused(capsys, tmp_path):
    real = tmp_path / "recorded.json"
    real.write_text('{"as_of": "2026-10-16"}', encoding="utf-8")
    assert run(capsys, "bill", "--fixture", str(real))[0] == cli.EXIT_INTERNAL


def test_unexpected_exception_is_internal_error(capsys, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret-looking detail")
    monkeypatch.setitem(cli.COMMANDS, "bill", boom)
    code, out, err = run(capsys, "bill")
    assert code == cli.EXIT_INTERNAL and out == "" and "secret-looking detail" not in err


def test_json_shape(capsys):
    _, data = run_json(capsys, "bill")
    assert set(data) == {"schema_version", "command", "mode", "as_of", "fixture", "exit_code", "speech",
                         "period", "report"}
    assert data["schema_version"] == "1" and data["command"] == "bill"
    _, data = run_json(capsys, "top-services")
    assert {"ranking", "period", "report"} <= set(data)
    _, data = run_json(capsys, "compare")
    r = data["report"]
    for key in ("basis", "earlier", "later", "earlier_month", "later_month", "charges_change", "net_change",
                "service_changes", "category_changes", "explanation", "warnings", "aws_requests", "generated_at"):
        assert key in r
    _, data = run_json(capsys, "anomalies")
    assert {"status", "window_start", "window_end", "monitor_count", "last_evaluated", "stale", "anomalies",
            "currency_basis"} <= set(data["report"])


# ---------- fixture mode needs no boto3, credentials or network ----------

def test_fixture_mode_imports_no_boto3_and_needs_no_credentials_or_network(tmp_path):
    script = """
import socket, sys
def no_network(*a, **k):
    raise AssertionError("network access attempted")
socket.socket.connect = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
from cloudguard.cost.__main__ import main
codes = [main([c, "--format", "json"]) for c in ("bill", "top-services", "compare", "explain-increase", "anomalies")]
codes.append(main(["compare", "--months", "september", "october", "--format", "json"]))
codes.append(main(["bill", "--fixture", "access-denied", "--format", "json"]))
assert "boto3" not in sys.modules, "boto3 was imported"
print("CODES", codes)
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update({"AWS_CONFIG_FILE": str(tmp_path / "none"), "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "none"),
                "AWS_EC2_METADATA_DISABLED": "true"})
    proc = subprocess.run([sys.executable, "-B", "-c", script], cwd=BACKEND, env=env, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "CODES [0, 0, 0, 0, 1, 0, 3]" in proc.stdout


def test_fixture_client_rejects_requests_the_code_never_sends():
    from cloudguard.cost.fixture import FixtureError
    client = FixtureCostExplorer.load("demo")
    with pytest.raises(FixtureError):
        client.get_cost_and_usage(TimePeriod={"Start": "2026-09-01", "End": "2026-10-01"}, Granularity="DAILY",
                                  Metrics=["UnblendedCost"], GroupBy=[])


# ---------- live mode (Stubber only; never AWS) ----------

GROUP_BY = [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "RECORD_TYPE"}]


def _stubbed(monkeypatch):
    import boto3
    from botocore.stub import Stubber

    from cloudguard.cost.ce import CE_CONFIG, CE_REGION
    client = boto3.client("ce", region_name=CE_REGION, config=CE_CONFIG, aws_access_key_id="testing",
                          aws_secret_access_key="testing")
    stub = Stubber(client)
    seen = {}

    def factory(profile):
        seen["profile"] = profile
        return client
    monkeypatch.setattr(cli, "_live_client", factory)
    return client, stub, seen


def _ce_params(start, end, token=None):
    p = {"TimePeriod": {"Start": start, "End": end}, "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"],
         "GroupBy": GROUP_BY}
    return p | ({"NextPageToken": token} if token else {})


def _g(service, record_type, amount):
    return {"Keys": [service, record_type], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}


def _r(start, end, groups, estimated=False):
    return {"TimePeriod": {"Start": start, "End": end}, "Total": {}, "Groups": groups, "Estimated": estimated}


def test_live_bill_paginates_through_cost_explorer_in_us_east_1(capsys, monkeypatch):
    client, stub, seen = _stubbed(monkeypatch)
    assert client.meta.endpoint_url == "https://ce.us-east-1.amazonaws.com"
    stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _r("2026-08-01", "2026-09-01", [_g("AWS Lambda", "Usage", "1.00")]),
        _r("2026-09-01", "2026-10-01", [_g("AWS Lambda", "Usage", "2.00")])], "NextPageToken": "p2"},
        _ce_params("2026-08-01", "2026-10-01"))
    stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _r("2026-09-01", "2026-10-01", [_g("Tax", "Tax", "0.36"), _g("AWS Lambda", "Credit", "-2.36")])]},
        _ce_params("2026-08-01", "2026-10-01", "p2"))
    with stub:
        code, data = run_json(capsys, "bill", "--mode", "live", "--as-of", "2026-10-16", "--profile", "readonly")
        stub.assert_no_pending_responses()
    assert code == 0 and data["mode"] == "live" and data["fixture"] is None and seen["profile"] == "readonly"
    m = data["report"]["month"]
    assert Decimal(m["charges_before_discounts_credits_and_refunds"]) == Decimal("2.36")
    assert Decimal(m["calculated_net"]) == 0 and data["report"]["aws_requests"] == 2


def test_live_month_to_date_requests_through_as_of(capsys, monkeypatch):
    client, stub, _ = _stubbed(monkeypatch)
    stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _r("2026-10-01", "2026-10-17", [_g("AWS Lambda", "Usage", "0.42")], estimated=True)]},
        _ce_params("2026-10-01", "2026-10-17"))
    with stub:
        code, data = run_json(capsys, "bill", "--mode", "live", "--as-of", "2026-10-16", "--period", "this-month")
        stub.assert_no_pending_responses()
    assert code == 0 and "not a final bill" in data["speech"]


def test_live_compare_matching_days_makes_two_requests(capsys, monkeypatch):
    client, stub, _ = _stubbed(monkeypatch)
    stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _r("2026-09-01", "2026-10-01", [_g("AWS Lambda", "Usage", "9.00")]),
        _r("2026-10-01", "2026-10-17", [_g("AWS Lambda", "Usage", "5.00")], estimated=True)]},
        _ce_params("2026-09-01", "2026-10-17"))
    stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _r("2026-09-01", "2026-09-17", [_g("AWS Lambda", "Usage", "4.00")])]},
        _ce_params("2026-09-01", "2026-09-17"))
    with stub:
        code, data = run_json(capsys, "explain-increase", "--mode", "live", "--as-of", "2026-10-16",
                              "--months", "2026-09", "2026-10")
        stub.assert_no_pending_responses()
    x = data["report"]["explanation"]
    assert code == 0 and Decimal(x["net_change"]) == Decimal("1.00") and x["drivers"][0]["name"] == "AWS Lambda"


@pytest.mark.parametrize("code_name,status", [("AccessDeniedException", "ACCESS_DENIED"),
                                              ("DataUnavailableException", "DATA_UNAVAILABLE"),
                                              ("LimitExceededException", "ERROR")])
def test_live_api_errors(capsys, monkeypatch, code_name, status):
    client, stub, _ = _stubbed(monkeypatch)
    stub.add_client_error("get_cost_and_usage", code_name, "User: arn:aws:iam::123456789012:user/x is not authorized",
                          400, expected_params=_ce_params("2026-08-01", "2026-10-01"))
    with stub:
        code, data = run_json(capsys, "bill", "--mode", "live", "--as-of", "2026-10-16")
    assert code == cli.EXIT_UNAVAILABLE and data["report"]["status"] == status
    assert "123456789012" not in json.dumps(data)


def test_live_anomalies_paginate_and_flag_findings(capsys, monkeypatch):
    client, stub, _ = _stubbed(monkeypatch)
    arn = "arn:aws:ce::123456789012:anomalymonitor/x"
    monitor = {"MonitorArn": arn, "MonitorName": "m", "MonitorType": "DIMENSIONAL", "MonitorDimension": "SERVICE",
               "LastEvaluatedDate": "2026-10-15"}
    window = {"DateInterval": {"StartDate": "2026-10-09", "EndDate": "2026-10-16"}}
    anomaly = {"AnomalyId": "a1", "AnomalyStartDate": "2026-10-12", "MonitorArn": arn,
               "AnomalyScore": {"MaxScore": 0.8, "CurrentScore": 0.4},
               "Impact": {"MaxImpact": 3.0, "TotalImpact": 3.0, "TotalActualSpend": 4.0, "TotalExpectedSpend": 1.0},
               "RootCauses": [{"Service": "AWS Lambda", "Impact": {"Contribution": 3.0}}]}
    stub.add_response("get_anomaly_monitors", {"AnomalyMonitors": [monitor]}, {})
    stub.add_response("get_anomalies", {"Anomalies": [], "NextPageToken": "n2"}, window)
    stub.add_response("get_anomalies", {"Anomalies": [anomaly]}, window | {"NextPageToken": "n2"})
    with stub:
        code, data = run_json(capsys, "anomalies", "--mode", "live", "--as-of", "2026-10-16")
        stub.assert_no_pending_responses()
    assert code == cli.EXIT_FINDINGS and data["report"]["aws_requests"] == 3
    assert "arn:" not in json.dumps(data)


def test_live_rejects_future_as_of(capsys, monkeypatch):
    _stubbed(monkeypatch)
    assert run(capsys, "bill", "--mode", "live", "--as-of", "2999-01-01")[0] == cli.EXIT_USAGE


def test_live_unknown_profile_is_aws_setup_error(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_live_client", REAL_LIVE_CLIENT)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    code, out, err = run(capsys, "bill", "--mode", "live", "--profile", "cloudguard-no-such-profile",
                         "--as-of", "2026-10-16")
    assert code == cli.EXIT_UNAVAILABLE and "ProfileNotFound" in err and out == ""
