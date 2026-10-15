"""Cost Anomaly Detection against botocore Stubber: real request/response shapes, no network."""

from datetime import UTC, date, datetime
from decimal import Decimal

import boto3
import pytest
from botocore.stub import Stubber

from cloudguard.cost.anomalies import get_cost_anomalies
from cloudguard.cost.ce import CE_REGION
from cloudguard.cost.models import ANOMALY_CURRENCY_BASIS, AnomalyStatus, ReportWarning
from cloudguard.cost.voice import anomalies_speech

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
RETRIEVED = datetime(2026, 10, 15, 9, 30, 2, tzinfo=UTC)
WINDOW = {"DateInterval": {"StartDate": "2026-10-08", "EndDate": "2026-10-15"}}
MONITOR_ARN = "arn:aws:ce::123456789012:anomalymonitor/abc"


def client():
    return boto3.client("ce", region_name=CE_REGION, aws_access_key_id="testing", aws_secret_access_key="testing")


def monitor(last_evaluated="2026-10-14"):
    m = {"MonitorArn": MONITOR_ARN, "MonitorName": "Default-Services-Monitor", "MonitorType": "DIMENSIONAL",
         "MonitorDimension": "SERVICE"}
    if last_evaluated:
        m["LastEvaluatedDate"] = last_evaluated
    return m


def anomaly(aid, total_impact, service="Amazon Elastic Compute Cloud - Compute", end="2026-10-12", **impact):
    a = {
        "AnomalyId": aid, "AnomalyStartDate": "2026-10-10", "MonitorArn": MONITOR_ARN,
        "AnomalyScore": {"MaxScore": 0.87, "CurrentScore": 0.5},
        "Impact": {"MaxImpact": 9.0, "TotalImpact": total_impact, **impact},
        "RootCauses": [
            {"Service": "AWS Lambda", "Region": "us-east-1", "LinkedAccount": "123456789012",
             "UsageType": "Request", "Impact": {"Contribution": 1.0}},
            {"Service": service, "Region": "ap-south-1", "LinkedAccount": "123456789012",
             "UsageType": "BoxUsage:t3.large", "Impact": {"Contribution": 20.0}},
        ],
    }
    if end:
        a["AnomalyEndDate"] = end
    return a


def run(monitors=None, anomalies=None, monitor_error=None, anomaly_error=None, lookback=7, **kwargs):
    c = client()
    with Stubber(c) as st:
        if monitor_error:
            st.add_client_error("get_anomaly_monitors", monitor_error, "arn:aws:iam::123456789012:user/x denied", 400)
        else:
            st.add_response("get_anomaly_monitors", {"AnomalyMonitors": monitors if monitors is not None else [monitor()]}, {})
        if anomaly_error:
            st.add_client_error("get_anomalies", anomaly_error, "denied", 400)
        elif anomalies is not None:
            st.add_response("get_anomalies", {"Anomalies": anomalies}, WINDOW)
        report = get_cost_anomalies(c, NOW, lookback, clock=lambda: RETRIEVED, **kwargs)
        st.assert_no_pending_responses()
    return report


def test_anomalies_found_are_parsed_and_sorted_by_impact():
    report = run(anomalies=[
        anomaly("small", 3.0, service="Amazon Simple Storage Service"),
        anomaly("big", 21.5, end=None, TotalExpectedSpend=4.0, TotalActualSpend=25.5, TotalImpactPercentage=537.5),
    ])
    assert report.status == AnomalyStatus.ANOMALIES_FOUND
    assert (report.window_start, report.window_end) == (date(2026, 10, 8), date(2026, 10, 15))
    assert report.monitor_count == 1 and report.last_evaluated == date(2026, 10, 14) and not report.stale
    assert report.aws_requests == 2 and report.retrieved_at == RETRIEVED and report.warnings == []
    big, small = report.anomalies
    assert big.service == "Amazon Elastic Compute Cloud - Compute"  # highest-contribution root cause
    assert big.region == "ap-south-1" and big.usage_type == "BoxUsage:t3.large"
    assert big.start_date == date(2026, 10, 10) and big.end_date is None
    assert big.expected_spend == Decimal("4.0") and big.actual_spend == Decimal("25.5")
    assert big.impact == Decimal("21.5") and big.impact_percent == Decimal("537.5") and big.score == Decimal("0.87")
    assert big.explanation == ("Amazon Elastic Compute Cloud - Compute cost 25.50 USD against an expected 4.00 USD, "
                               "21.50 USD above the baseline (537.5% higher). AWS still reports it as ongoing.")
    assert small.service == "Amazon Simple Storage Service" and small.end_date == date(2026, 10, 12)
    assert small.expected_spend is None and small.impact_percent is None
    assert small.explanation == "Amazon Simple Storage Service cost more than AWS expected, 3.00 USD above the baseline."

    speech = anomalies_speech(report)
    assert speech.startswith("AWS Cost Anomaly Detection reported 2 anomalies between October 8 and October 15. The largest: ")
    assert speech.endswith("AWS last evaluated your costs on October 14.")
    dumped = report.model_dump_json()
    assert "123456789012" not in dumped and "arn:" not in dumped


def test_currency_assumption_is_exposed():
    report = run(anomalies=[])
    assert report.currency == "USD" and report.currency_basis == ANOMALY_CURRENCY_BASIS
    assert report.currency_basis.startswith("documented_not_returned")
    assert report.currency_reference.endswith("API_Impact.html")


def test_bill_currency_mismatch_warns_without_converting():
    report = run(anomalies=[anomaly("x", 3.0)], bill_currency="INR")
    assert ReportWarning.CURRENCY_MISMATCH in report.warnings
    assert report.currency == "USD" and report.anomalies[0].impact == Decimal("3.0")
    assert run(anomalies=[], bill_currency="USD").warnings == []


def test_zero_expected_spend_explains_missing_percentage():
    report = run(anomalies=[anomaly("new", 5.0, TotalExpectedSpend=0.0, TotalActualSpend=5.0)])
    a = report.anomalies[0]
    assert a.impact_percent is None
    assert "(AWS gives no percentage because expected spend was zero)" in a.explanation


def test_total_impact_missing_falls_back_to_max_impact():
    a = anomaly("x", 0)
    del a["Impact"]["TotalImpact"]
    assert run(anomalies=[a]).anomalies[0].impact == Decimal("9.0")


def test_no_anomalies_is_not_called_normal_spending():
    report = run(anomalies=[])
    assert report.status == AnomalyStatus.NO_ANOMALIES and report.anomalies == []
    speech = anomalies_speech(report)
    assert "reported no anomalies between October 8 and October 15" in speech
    assert "doesn't guarantee spending is normal" in speech and "isn't real-time" in speech


def test_no_monitor_skips_the_anomaly_call():
    report = run(monitors=[])
    assert report.status == AnomalyStatus.NO_MONITOR and report.monitor_count == 0 and report.aws_requests == 1
    assert "No Cost Anomaly Detection monitor" in anomalies_speech(report)


def test_never_evaluated_monitor_is_not_yet_evaluated():
    report = run(monitors=[monitor(last_evaluated=None)])
    assert report.status == AnomalyStatus.NOT_YET_EVALUATED and report.last_evaluated is None
    assert report.aws_requests == 1


def test_delayed_evaluation_is_flagged_stale():
    report = run(monitors=[monitor("2026-10-10T00:00:00Z"), monitor("2026-10-12")], anomalies=[])
    assert report.last_evaluated == date(2026, 10, 12) and report.stale and report.monitor_count == 2
    assert report.warnings == [ReportWarning.STALE_EVALUATION]
    assert "more than two days ago" in anomalies_speech(report)


@pytest.mark.parametrize("which", ["monitor_error", "anomaly_error"])
def test_missing_anomaly_permissions(which):
    report = run(**{which: "AccessDeniedException"})
    assert report.status == AnomalyStatus.ACCESS_DENIED
    assert report.error.operation == ("ce:GetAnomalyMonitors" if which == "monitor_error" else "ce:GetAnomalies")
    assert report.error.inferred_possible_causes and report.error.aws_description is None
    assert "123456789012" not in report.model_dump_json()
    assert anomalies_speech(report) == "I don't have permission to read AWS Cost Anomaly Detection results."


def test_other_failures_are_errors():
    report = run(anomaly_error="LimitExceededException")
    assert report.status == AnomalyStatus.ERROR and report.error.code == "LimitExceededException"
    assert report.error.aws_description.startswith("You made too many calls")
    assert report.last_evaluated == date(2026, 10, 14) and report.aws_requests == 2


def test_anomaly_page_limit_refuses_partial_results():
    c = client()
    with Stubber(c) as st:
        st.add_response("get_anomaly_monitors", {"AnomalyMonitors": [monitor()]}, {})
        st.add_response("get_anomalies", {"Anomalies": [anomaly("a", 1.0)], "NextPageToken": "n1"}, WINDOW)
        report = get_cost_anomalies(c, NOW, max_pages=1, clock=lambda: RETRIEVED)
    assert report.status == AnomalyStatus.ERROR and report.error.code == "PageLimitExceeded"
    assert report.anomalies == [] and report.aws_requests == 2


@pytest.mark.parametrize("days", [0, 91])
def test_lookback_is_bounded(days):
    with pytest.raises(ValueError):
        get_cost_anomalies(client(), NOW, days)
