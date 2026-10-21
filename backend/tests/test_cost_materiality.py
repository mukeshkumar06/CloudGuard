"""Regressions from live verification (2026-10-16): near-zero net speech and attribution of numerical noise.

Live Cost Explorer amounts carry ~10 decimal places. When credits cover usage, a month's calculated net was
-3.8E-9 (spoken as "a net credit of 0.00") and a matching-days net change of +1.5E-9 was classified as
INCREASE_ATTRIBUTED. These tests pin the speech tolerance and the materiality threshold.
"""

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import cloudguard.cost.__main__ as cli
from cloudguard.cost.bill import _Month
from cloudguard.cost.compare import explain_change, get_comparison, service_changes
from cloudguard.cost.fixture import FixtureCostExplorer
from cloudguard.cost.models import (
    MATERIALITY_THRESHOLD, BillReport, BillStatus, IncreaseVerdict, MonthComparison,
)
from cloudguard.cost.voice import SPEECH_NET_TOLERANCE, bill_speech, comparison_speech, negligible, net_phrase
from cloudguard.server.app import create_server
from cloudguard.server.context import CostContext

D = Decimal


def month(*lines, start=date(2026, 9, 1)):
    m = _Month()
    m.end = date(2026, 10, 1)
    for service, record_type, amount in lines:
        m.add(service, record_type, D(amount))
    return m.to_model(start)


def explain(earlier_lines, later_lines):
    e, l = month(*earlier_lines), month(*later_lines)
    x = explain_change(e, l, service_changes(e, l))
    parts = sum((d.change for d in x.drivers + x.offsets), D(0)) + x.below_threshold_change
    assert parts == x.net_change == l.calculated_net - e.calculated_net  # exact split, nothing dropped
    return x


# ---------- Bug 1: sign-aware, tolerance-aware net speech ----------

def test_constants_are_documented_values():
    assert SPEECH_NET_TOLERANCE == D("0.005") and MATERIALITY_THRESHOLD == D("0.01")


@pytest.mark.parametrize("value,expected", [
    ("0.25", "a net charge of 0.25"),
    ("12.345", "a net charge of 12.35"),
    ("-0.50", "a net credit of 0.50"),
    ("0.01", "a net charge of 0.01"),  # small but sayable
    ("-0.01", "a net credit of 0.01"),
    ("0", "a net of zero"),
    ("0E-10", "a net of zero"),
    ("-0", "a net of zero"),
])
def test_net_phrase_signed_from_value(value, expected):
    assert net_phrase(D(value)) == expected


@pytest.mark.parametrize("value", ["-3.8E-9", "1.5E-9", "0.004", "-0.004", "0.0049999999", "-0.0049999999"])
def test_values_that_round_to_zero_are_approximately_zero_not_signed(value):
    phrase = net_phrase(D(value))
    assert phrase == "a net of approximately zero (charges and credits approximately balance)"
    assert "0.00" not in phrase and "credit of" not in phrase and "charge of" not in phrase
    assert negligible(D(value))


@pytest.mark.parametrize("value,expected", [("0.005", "a net charge of 0.01"), ("-0.005", "a net credit of 0.01")])
def test_tolerance_boundary_is_exclusive(value, expected):
    assert not negligible(D(value)) and net_phrase(D(value)) == expected


def test_near_zero_bill_speech_json_and_summary(capsys):
    code = cli.main(["bill", "--fixture", "near-zero-net", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    m = data["report"]["month"]
    assert D(m["calculated_net"]) == D("-4.0E-9")  # exact, not rounded or forced to zero
    assert D(m["credits"]) == D("-0.2403000040") and D(m["usage"]) == D("0.2403000000")
    speech = data["speech"]
    assert "a net of approximately zero (charges and credits approximately balance)" in speech
    assert "net credit of 0.00" not in speech and "net charge of 0.00" not in speech
    cli.main(["bill", "--fixture", "near-zero-net"])
    text = capsys.readouterr().out
    assert not re.search(r"-0\.00(?!\d)", text) and "approximately zero; exact -0.0000000040" in text


def test_bill_comparison_with_sub_cent_change_is_about_the_same():
    m = month(("AWS Lambda", "Usage", "1.00"))
    report = BillReport(status=BillStatus.OK, period_start=date(2026, 9, 1), period_end_exclusive=date(2026, 10, 1),
                        generated_at=datetime(2026, 10, 16, tzinfo=UTC), currency="USD", month=m,
                        comparison=MonthComparison(previous_start=date(2026, 8, 1), previous_charges=D("0.9999999990"),
                                                   change=D("1.0E-9"), change_percent=D("0.0"), estimated=False))
    speech = bill_speech(report)
    assert "about the same as August, less than 0.01 apart" in speech and "0.00 more" not in speech


# ---------- Bug 2: materiality threshold for attribution ----------

BASE = [("Amazon Simple Storage Service", "Usage", "0.20")]


def test_tiny_positive_change_is_not_material():
    x = explain(BASE + [("Amazon Simple Storage Service", "Credit", "-0.2000000030")],
                BASE + [("Amazon Simple Storage Service", "Credit", "-0.2000000015")])
    assert x.net_change == D("1.5E-9") and x.verdict == IncreaseVerdict.NO_MATERIAL_CHANGE
    assert x.drivers == [] and x.offsets == [] and x.below_threshold_change == D("1.5E-9")


def test_tiny_negative_change_is_not_material():
    x = explain(BASE, [("Amazon Simple Storage Service", "Usage", "0.1999999962")])
    assert x.net_change == D("-3.8E-9") and x.verdict == IncreaseVerdict.NO_MATERIAL_CHANGE


def test_exact_zero_change():
    x = explain(BASE, BASE)
    assert x.net_change == 0 and x.verdict == IncreaseVerdict.NO_MATERIAL_CHANGE and x.gross_increase == 0


@pytest.mark.parametrize("later,verdict", [
    ("0.2099999999", IncreaseVerdict.NO_MATERIAL_CHANGE),  # +0.0099999999: just below
    ("0.21", IncreaseVerdict.INCREASE_ATTRIBUTED),  # +0.01: at the threshold, material
    ("0.2100000001", IncreaseVerdict.INCREASE_ATTRIBUTED),  # just above
    ("0.1900000001", IncreaseVerdict.NO_MATERIAL_CHANGE),  # -0.0099999999
    ("0.19", IncreaseVerdict.NO_INCREASE),  # -0.01
])
def test_threshold_boundaries(later, verdict):
    x = explain(BASE, [("Amazon Simple Storage Service", "Usage", later)])
    assert x.verdict == verdict
    if verdict == IncreaseVerdict.INCREASE_ATTRIBUTED:
        assert [d.name for d in x.drivers] == ["Amazon Simple Storage Service"]


def test_many_sub_threshold_service_changes_that_add_up_are_not_attributed():
    services = ["AWS Lambda", "AWS AppSync", "Amazon DynamoDB", "AmazonCloudWatch"]
    x = explain([(s, "Usage", "0.10") for s in services], [(s, "Usage", "0.104") for s in services])
    assert x.net_change == D("0.016") and x.verdict == IncreaseVerdict.CANNOT_DETERMINE
    assert x.drivers == [] and x.below_threshold_change == D("0.016")
    assert "each smaller than 0.01" in x.reasons[0]


def test_meaningful_increase_alongside_numerical_noise():
    x = explain([("Amazon Elastic Compute Cloud - Compute", "Usage", "10.00"), ("AWS Lambda", "Usage", "0.10"),
                 ("AWS Lambda", "Credit", "-0.1000000020")],
                [("Amazon Elastic Compute Cloud - Compute", "Usage", "15.00"), ("AWS Lambda", "Usage", "0.10"),
                 ("AWS Lambda", "Credit", "-0.1000000005")])
    assert x.verdict == IncreaseVerdict.INCREASE_ATTRIBUTED
    assert [(d.name, d.change) for d in x.drivers] == [("Amazon Elastic Compute Cloud - Compute", D("5.00"))]
    assert x.offsets == [] and x.below_threshold_change == D("1.5E-9")


def test_legitimate_small_increase_is_preserved():
    x = explain([("AWS Lambda", "Usage", "0.05")], [("AWS Lambda", "Usage", "0.07")])
    assert x.verdict == IncreaseVerdict.INCREASE_ATTRIBUTED and x.drivers[0].change == D("0.02")


def test_offsetting_material_changes_with_immaterial_net():
    """The live case: credits shrank by ~0.026 while usage fell by the same amount."""
    x = explain([("S3", "Usage", "0.13"), ("S3", "Credit", "-0.1300000030")],
                [("S3", "Usage", "0.097"), ("S3", "Credit", "-0.0970000015")])
    assert x.verdict == IncreaseVerdict.NO_MATERIAL_CHANGE
    assert {d.name for d in x.drivers} == {"credits"} and {d.name for d in x.offsets} == {"S3"}


# ---------- one verdict across JSON, speech, summary and MCP ----------

@pytest.mark.parametrize("months", [None, ("september", "october")])
def test_verdict_consistent_across_outputs(capsys, months):
    extra = ["--months", *months] if months else []
    assert cli.main(["explain-increase", "--fixture", "near-zero-net", "--format", "json", *extra]) == 0
    data = json.loads(capsys.readouterr().out)
    x = data["report"]["explanation"]
    assert x["verdict"] == "NO_MATERIAL_CHANGE" and D(x["materiality_threshold"]) == D("0.01")
    assert "didn't change materially" in data["speech"] and "rose by 0.00" not in data["speech"]
    assert "fell by 0.00" not in data["speech"]
    cli.main(["explain-increase", "--fixture", "near-zero-net", *extra])
    text = capsys.readouterr().out
    assert "Attribution: NO_MATERIAL_CHANGE" in text and "Materiality threshold 0.01 USD" in text

    async def via_mcp():
        server = create_server(CostContext.fixture("near-zero-net"))
        async with create_connected_server_and_client_session(server) as client:
            args = {"earlier_month": months[0], "later_month": months[1]} if months else {}
            return await client.call_tool("investigate_cost_increase", args)
    result = anyio.run(via_mcp)
    assert not result.isError
    sc = result.structuredContent
    assert sc["report"]["explanation"] == x and sc["speech"] == data["speech"]


def test_compare_speech_near_zero_nets():
    c = get_comparison(FixtureCostExplorer.load("near-zero-net"), date(2026, 9, 1), date(2026, 10, 1),
                       date(2026, 10, 16), datetime(2026, 10, 16, tzinfo=UTC))
    speech = comparison_speech(c)
    assert "0.00" not in speech.split("For context")[0].split("After discounts")[1]
    assert "approximately balance in both periods" in speech and "credit of 0.00" not in speech
