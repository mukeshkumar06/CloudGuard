"""Golden sample responses in docs/samples/cost/, built from synthetic (stubbed) AWS responses.

Each sample is {"description", "speech", "report"}. The test fails if code changes alter a sample.
After an intended change, regenerate with CLOUDGUARD_UPDATE_SAMPLES=1 and review the diff.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from cloudguard.cost.anomalies import get_cost_anomalies
from cloudguard.cost.bill import get_last_month_bill
from cloudguard.cost.ce import CE_REGION
from cloudguard.cost.voice import anomalies_speech, bill_speech

SAMPLES = Path(__file__).resolve().parents[2] / "docs" / "samples" / "cost"
NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
RETRIEVED = datetime(2026, 10, 15, 9, 30, 1, tzinfo=UTC)
ACCOUNT = "123456789012"  # synthetic; must never appear in a sample
MONITOR_ARN = f"arn:aws:ce::{ACCOUNT}:anomalymonitor/0000-synthetic"
AUG = {"Start": "2026-08-01", "End": "2026-09-01"}
SEP = {"Start": "2026-09-01", "End": "2026-10-01"}


def g(service, record_type, amount):
    return {"Keys": [service, record_type], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}


def months(aug_groups, sep_groups, sep_estimated=False):
    return {"ResultsByTime": [
        {"TimePeriod": AUG, "Total": {}, "Groups": aug_groups, "Estimated": False},
        {"TimePeriod": SEP, "Total": {}, "Groups": sep_groups, "Estimated": sep_estimated},
    ]}


AUGUST = [g("Amazon Elastic Compute Cloud - Compute", "Usage", "11.2000000000"),
          g("Amazon Simple Storage Service", "Usage", "2.1000000000"),
          g("Tax", "Tax", "2.3940000000")]

BILL_INPUTS = {
    "bill-success": ("Successful bill: usage, a Savings Plan pair, a support fee and tax; no credits.", months(AUGUST, [
        g("Amazon Elastic Compute Cloud - Compute", "Usage", "14.6200000000"),
        g("Amazon Elastic Compute Cloud - Compute", "Savings Plan covered usage", "3.0000000000"),
        g("Amazon Elastic Compute Cloud - Compute", "Savings Plan negation", "-3.0000000000"),
        g("Savings Plans for AWS Compute usage", "Savings Plan recurring fee", "2.1900000000"),
        g("Amazon Simple Storage Service", "Usage", "2.4800000000"),
        g("AWS Lambda", "Usage", "0.3100000000"),
        g("AWS Support (Developer)", "Support fee", "29.0000000000"),
        g("Tax", "Tax", "8.6976000000"),
    ])),
    "bill-with-credits": ("Bill fully covered by promotional credits, with one refund; still estimated by AWS.", months(AUGUST, [
        g("Amazon Elastic Compute Cloud - Compute", "Usage", "9.4000000000"),
        g("Amazon Simple Storage Service", "Usage", "1.2500000000"),
        g("Amazon DynamoDB", "Usage", "0.4200000000"),
        g("Tax", "Tax", "1.9926000000"),
        g("Amazon Elastic Compute Cloud - Compute", "Credit", "-9.4000000000"),
        g("Amazon Simple Storage Service", "Credit", "-1.2500000000"),
        g("Amazon DynamoDB", "Credit", "-0.4200000000"),
        g("Tax", "Credit", "-1.9926000000"),
        g("Amazon Simple Storage Service", "Refund", "-0.5000000000"),
    ], sep_estimated=True)),
    "bill-unclassified": ("Bill containing record types CloudGuard does not recognise.", months(AUGUST, [
        g("Amazon Elastic Compute Cloud - Compute", "Usage", "12.0000000000"),
        g("Amazon Simple Storage Service", "Usage", "1.7500000000"),
        g("Tax", "Tax", "2.4750000000"),
        g("Amazon Elastic Compute Cloud - Compute", "Bundled discount", "-0.6000000000"),
        g("AWS Marketplace", "Marketplace adjustment", "3.4000000000"),
        g("Amazon Simple Storage Service", "Synthetic future type", "-0.1500000000"),
    ])),
}


def monitor(last_evaluated="2026-10-14"):
    return {"MonitorArn": MONITOR_ARN, "MonitorName": "Default-Services-Monitor", "MonitorType": "DIMENSIONAL",
            "MonitorDimension": "SERVICE", "LastEvaluatedDate": last_evaluated}


ANOMALIES = [
    {"AnomalyId": "synthetic-1", "AnomalyStartDate": "2026-10-11", "MonitorArn": MONITOR_ARN,
     "AnomalyScore": {"MaxScore": 0.91, "CurrentScore": 0.62},
     "Impact": {"MaxImpact": 3.1, "TotalImpact": 6.4, "TotalActualSpend": 7.2, "TotalExpectedSpend": 0.8,
                "TotalImpactPercentage": 800.0},
     "RootCauses": [{"Service": "Amazon Simple Storage Service", "Region": "ap-south-1", "LinkedAccount": ACCOUNT,
                     "UsageType": "APS3-Requests-Tier1", "Impact": {"Contribution": 6.4}}]},
    {"AnomalyId": "synthetic-2", "AnomalyStartDate": "2026-10-09", "AnomalyEndDate": "2026-10-10",
     "MonitorArn": MONITOR_ARN, "AnomalyScore": {"MaxScore": 0.74, "CurrentScore": 0.0},
     "Impact": {"MaxImpact": 2.0, "TotalImpact": 2.0, "TotalActualSpend": 2.0, "TotalExpectedSpend": 0.0},
     "RootCauses": [{"Service": "Amazon Bedrock", "Region": "ap-south-1", "LinkedAccount": ACCOUNT,
                     "UsageType": "APS3-input-tokens", "Impact": {"Contribution": 2.0}}]},
]
WINDOW = {"DateInterval": {"StartDate": "2026-10-08", "EndDate": "2026-10-15"}}


def client():
    return boto3.client("ce", region_name=CE_REGION, aws_access_key_id="testing", aws_secret_access_key="testing")


def build_bill(response=None, error=None):
    c = client()
    with Stubber(c) as st:
        if error:
            st.add_client_error("get_cost_and_usage", error, f"User: arn:aws:iam::{ACCOUNT}:user/synthetic is not authorized", 400)
        else:
            st.add_response("get_cost_and_usage", response)
        report = get_last_month_bill(c, NOW, clock=lambda: RETRIEVED)
    return report, bill_speech(report)


def build_anomalies(monitors, anomalies=None, error=None):
    c = client()
    with Stubber(c) as st:
        if error:
            st.add_client_error("get_anomaly_monitors", error, f"User: arn:aws:iam::{ACCOUNT}:user/synthetic is not authorized", 400)
        else:
            st.add_response("get_anomaly_monitors", {"AnomalyMonitors": monitors})
            if anomalies is not None:
                st.add_response("get_anomalies", {"Anomalies": anomalies}, WINDOW)
        report = get_cost_anomalies(c, NOW, clock=lambda: RETRIEVED, bill_currency="USD")
    return report, anomalies_speech(report)


def samples():
    for name, (description, response) in BILL_INPUTS.items():
        yield name, description, lambda r=response: build_bill(r)
    yield ("bill-access-denied", "Bill request rejected because the identity lacks ce:GetCostAndUsage.",
           lambda: build_bill(error="AccessDeniedException"))
    yield ("anomalies-found", "Two anomalies: one ongoing, one closed with zero expected spend.",
           lambda: build_anomalies([monitor()], ANOMALIES))
    yield ("anomalies-none", "Monitor evaluated recently; AWS returned no anomalies for the window.",
           lambda: build_anomalies([monitor()], []))
    yield ("anomalies-no-monitor", "No Cost Anomaly Detection monitor is configured.",
           lambda: build_anomalies([]))
    yield ("anomalies-access-denied", "Anomaly request rejected because the identity lacks ce:GetAnomalyMonitors.",
           lambda: build_anomalies([], error="AccessDeniedException"))


CASES = list(samples())


@pytest.mark.parametrize("name,description,build", CASES, ids=[c[0] for c in CASES])
def test_sample_matches_golden_file(name, description, build):
    report, speech = build()
    sample = {"description": description, "speech": speech, "report": report.model_dump(mode="json")}
    text = json.dumps(sample, indent=2, ensure_ascii=False) + "\n"
    assert ACCOUNT not in text and "arn:" not in text
    path = SAMPLES / f"{name}.json"
    if os.environ.get("CLOUDGUARD_UPDATE_SAMPLES") == "1":
        SAMPLES.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"missing {path}; run with CLOUDGUARD_UPDATE_SAMPLES=1"
    assert json.loads(path.read_text(encoding="utf-8")) == sample
