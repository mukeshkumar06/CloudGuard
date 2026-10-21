"""Last-month bill against botocore Stubber: real Cost Explorer request/response shapes, no network."""

import random
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import NoCredentialsError
from botocore.stub import Stubber

from cloudguard.cost.bill import get_last_month_bill
from cloudguard.cost.ce import CE_REGION, MAX_PAGES_ENV, resolve_max_pages
from cloudguard.cost.models import NET_BASIS, BillStatus, ReportWarning, ServiceCost
from cloudguard.cost.periods import month_before, previous_billing_month
from cloudguard.cost.record_types import Category
from cloudguard.cost.voice import bill_speech

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
RETRIEVED = datetime(2026, 10, 15, 9, 30, 2, tzinfo=UTC)
IST = timezone(timedelta(hours=5, minutes=30))
PDT = timezone(timedelta(hours=-7))
GROUP_BY = [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "RECORD_TYPE"}]


def params(start="2026-08-01", end="2026-10-01", token=None):
    p = {"TimePeriod": {"Start": start, "End": end}, "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"],
         "GroupBy": GROUP_BY}
    return p | ({"NextPageToken": token} if token else {})


def client():
    return boto3.client("ce", region_name=CE_REGION, aws_access_key_id="testing", aws_secret_access_key="testing")


def g(service, record_type, amount, unit="USD"):
    return {"Keys": [service, record_type], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": unit}}}


def month(start, end, groups, estimated=False):
    return {"TimePeriod": {"Start": start, "End": end}, "Total": {}, "Groups": groups, "Estimated": estimated}


AUG = "2026-08-01", "2026-09-01"
SEP = "2026-09-01", "2026-10-01"


def run(*responses, errors=(), now=NOW, **kwargs):
    c = client()
    with Stubber(c) as st:
        for i, resp in enumerate(responses):
            st.add_response("get_cost_and_usage", resp, params(token=f"t{i}" if i else None))
        for code, status in errors:
            st.add_client_error("get_cost_and_usage", code,
                                "User: arn:aws:iam::123456789012:user/x is not authorized", status)
        report = get_last_month_bill(c, now, clock=lambda: RETRIEVED, **kwargs)
        st.assert_no_pending_responses()
    return report


def assert_reconciles(m):
    """Every returned amount is in exactly one category, and every total adds up."""
    charges = m.usage + m.savings_plan_negation + m.fees + m.tax
    assert m.charges_before_discounts_credits_and_refunds == charges
    assert m.calculated_net == charges + m.discounts + m.credits + m.refunds + m.unclassified
    assert m.calculated_net == sum((r.amount for r in m.record_types), Decimal(0))
    assert sum((s.amount for s in m.services), Decimal(0)) == charges
    for category, field in [(Category.USAGE, m.usage), (Category.SAVINGS_PLAN_NEGATION, m.savings_plan_negation),
                            (Category.FEE, m.fees), (Category.TAX, m.tax), (Category.DISCOUNT, m.discounts),
                            (Category.CREDIT, m.credits), (Category.REFUND, m.refunds),
                            (Category.UNCLASSIFIED, m.unclassified)]:
        assert sum((r.amount for r in m.record_types if r.category == category), Decimal(0)) == field


# ---------- periods (canonical UTC billing month) ----------

@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 10, 15, tzinfo=UTC), (date(2026, 9, 1), date(2026, 10, 1))),
    (datetime(2026, 10, 1, 0, 0, tzinfo=UTC), (date(2026, 9, 1), date(2026, 10, 1))),
    (datetime(2026, 9, 30, 23, 59, tzinfo=UTC), (date(2026, 8, 1), date(2026, 9, 1))),
    (datetime(2026, 1, 5, tzinfo=UTC), (date(2025, 12, 1), date(2026, 1, 1))),
    (datetime(2028, 3, 1, tzinfo=UTC), (date(2028, 2, 1), date(2028, 3, 1))),
    # The caller's timezone never shifts AWS's UTC month boundaries:
    (datetime(2026, 10, 1, 3, 0, tzinfo=IST), (date(2026, 8, 1), date(2026, 9, 1))),  # still 30 Sep in UTC
    (datetime(2026, 10, 1, 6, 0, tzinfo=IST), (date(2026, 9, 1), date(2026, 10, 1))),  # 1 Oct 00:30 UTC
    (datetime(2026, 1, 1, 1, 0, tzinfo=IST), (date(2025, 11, 1), date(2025, 12, 1))),  # still 31 Dec in UTC
    (datetime(2026, 9, 30, 20, 0, tzinfo=PDT), (date(2026, 9, 1), date(2026, 10, 1))),  # already 1 Oct in UTC
])
def test_previous_billing_month_is_utc(now, expected):
    assert previous_billing_month(now) == expected


def test_naive_datetime_is_refused():
    with pytest.raises(ValueError):
        previous_billing_month(datetime(2026, 10, 15))


def test_month_before_crosses_year():
    assert month_before(date(2026, 1, 1)) == (date(2025, 12, 1), date(2026, 1, 1))


def test_request_uses_date_only_strings_for_utc_month():
    c = client()
    with Stubber(c) as st:
        st.add_response("get_cost_and_usage", {"ResultsByTime": []}, params("2026-07-01", "2026-09-01"))
        report = get_last_month_bill(c, datetime(2026, 10, 1, 3, 0, tzinfo=IST), clock=lambda: RETRIEVED)
        st.assert_no_pending_responses()
    assert report.period_start == date(2026, 8, 1) and report.billing_timezone == "UTC"
    assert "UTC" in report.period_basis


# ---------- classification and reconciliation ----------

def test_every_category_is_separated_and_reconciles():
    report = run({"ResultsByTime": [
        month(*AUG, [g("Amazon Simple Storage Service", "Usage", "4.00")]),
        month(*SEP, [
            g("Amazon Elastic Compute Cloud - Compute", "Usage", "6.50"),
            g("Amazon Elastic Compute Cloud - Compute", "Savings Plan Covered Usage", "3.00"),
            g("Amazon Elastic Compute Cloud - Compute", "SavingsPlanNegation", "-2.00"),
            g("Amazon Simple Storage Service", "Usage", "2.25"),
            g("AWS Support (Developer)", "Support fee", "29.00"),
            g("Tax", "Tax", "0.50"),
            g("Amazon Elastic Compute Cloud - Compute", "Bundled Discount", "-0.30"),
            g("Amazon Elastic Compute Cloud - Compute", "Credit", "-6.50"),
            g("AWS Lambda", "Credit", "-0.75"),
            g("Amazon Simple Storage Service", "Refund", "-1.00"),
            g("Amazon Simple Storage Service", "Mystery adjustment", "0.40"),
            g("AWS Glue", "Usage", "0"),
        ]),
    ]})
    assert report.status == BillStatus.OK and report.metric == "UnblendedCost" and report.currency == "USD"
    m = report.month
    assert (m.start, m.end_exclusive) == (date(2026, 9, 1), date(2026, 10, 1))
    assert (m.usage, m.savings_plan_negation, m.fees, m.tax) == (Decimal("11.75"), Decimal("-2.00"), Decimal("29.00"), Decimal("0.50"))
    assert m.charges_before_discounts_credits_and_refunds == Decimal("39.25")
    assert (m.discounts, m.credits, m.refunds) == (Decimal("-0.30"), Decimal("-7.25"), Decimal("-1.00"))
    assert m.unclassified == Decimal("0.40") and m.unclassified_record_types == ["Mystery adjustment"]
    assert m.calculated_net == Decimal("31.10") and m.net_basis == NET_BASIS
    assert_reconciles(m)
    # Unknown record types are never charges, and never in a service total.
    assert [(s.service, s.amount) for s in m.services] == [
        ("AWS Support (Developer)", Decimal("29.00")),
        ("Amazon Elastic Compute Cloud - Compute", Decimal("7.50")),
        ("Amazon Simple Storage Service", Decimal("2.25")),
        ("Tax", Decimal("0.50")),
    ]
    lines = {r.record_type: (r.category, r.match_source) for r in m.record_types}
    assert lines["SavingsPlanNegation"] == (Category.SAVINGS_PLAN_NEGATION, "console_doc")
    assert lines["Bundled Discount"] == (Category.DISCOUNT, "candidate")
    assert lines["Mystery adjustment"] == (Category.UNCLASSIFIED, None)
    assert report.warnings == [ReportWarning.UNCLASSIFIED_RECORD_TYPES]
    c = report.comparison
    assert c.previous_charges == Decimal("4.00") and c.change == Decimal("35.25") and c.change_percent == Decimal("881.3")

    speech = bill_speech(report)
    assert speech.startswith("For the September 2026 AWS billing month, charges before discounts, credits and "
                             "refunds were 39.25 US dollars, measured as unblended cost.")
    assert ("After discounts of 0.30, credits of 7.25 and refunds of 1.00, CloudGuard calculates a net charge of 31.10. "
            "That's a CloudGuard calculation and may differ from your invoice.") in speech
    assert ("Warning: 1 type of billing entry could not be classified. It totals 0.40. Unclassified amounts are "
            "not counted as charges but are included in CloudGuard's net.") in speech
    assert ("The top services were AWS Support (Developer) at 29.00, Amazon Elastic Compute Cloud - Compute at 7.50 "
            "and Amazon Simple Storage Service at 2.25.") in speech
    assert "That's 35.25 more than August, up 881.3 percent." in speech


# ---------- voice: tax exclusion and unclassified warning ----------

def test_tax_is_kept_in_report_but_not_spoken_as_a_top_service():
    report = run({"ResultsByTime": [month(*SEP, [
        g("Tax", "Tax", "9.00"),
        g("AWS Lambda", "Usage", "2.00"),
        g("Amazon Simple Storage Service", "Usage", "1.00"),
    ])]})
    m = report.month
    assert m.tax == Decimal("9.00") and m.charges_before_discounts_credits_and_refunds == Decimal("12.00")
    assert [(s.service, s.tax_only) for s in m.services] == [
        ("Tax", True), ("AWS Lambda", False), ("Amazon Simple Storage Service", False)]
    assert_reconciles(m)
    speech = bill_speech(report)
    assert "Tax at" not in speech
    assert "The top services were AWS Lambda at 2.00 and Amazon Simple Storage Service at 1.00." in speech
    assert "were 12.00 US dollars" in speech  # tax still counts in the spoken total


def test_tax_only_month_names_no_top_service():
    report = run({"ResultsByTime": [month(*SEP, [g("Tax", "Tax", "0.18")])]})
    assert report.month.services[0].tax_only
    assert "top service" not in bill_speech(report)


def test_service_with_usage_and_tax_lines_is_not_tax_only():
    report = run({"ResultsByTime": [month(*SEP, [g("AWS Lambda", "Usage", "1.00"), g("AWS Lambda", "Tax", "0.18")])]})
    assert report.month.services == [ServiceCost(service="AWS Lambda", amount=Decimal("1.18"), tax_only=False)]


def test_unclassified_warning_is_spoken_and_structured_even_when_amounts_net_to_zero():
    report = run({"ResultsByTime": [month(*SEP, [
        g("AWS Lambda", "Usage", "1.00"),
        g("AWS Lambda", "Brand new type", "0.50"),
        g("AWS Lambda", "Another new type", "-0.50"),
    ])]})
    m = report.month
    assert m.unclassified == 0 and sorted(m.unclassified_record_types) == ["Another new type", "Brand new type"]
    assert report.warnings == [ReportWarning.UNCLASSIFIED_RECORD_TYPES]
    assert_reconciles(m)
    speech = bill_speech(report)
    assert ("Warning: 2 types of billing entries could not be classified. Together they total 0.00.") in speech
    assert "CloudGuard calculates a net charge of 1.00" in speech


def test_no_unclassified_warning_when_everything_is_classified():
    report = run({"ResultsByTime": [month(*SEP, [g("AWS Lambda", "Usage", "1.00")])]})
    assert report.warnings == [] and "Warning" not in bill_speech(report)


def test_savings_plan_covered_usage_and_negation_cancel():
    report = run({"ResultsByTime": [month(*SEP, [
        g("Amazon Elastic Compute Cloud - Compute", "Savings Plan covered usage", "5.00"),
        g("Amazon Elastic Compute Cloud - Compute", "Savings Plan negation", "-5.00"),
        g("Savings Plans for AWS Compute usage", "Savings Plan recurring fee", "4.10"),
    ])]})
    m = report.month
    assert m.usage == 5 and m.savings_plan_negation == -5 and m.fees == Decimal("4.10")
    assert m.charges_before_discounts_credits_and_refunds == Decimal("4.10")
    assert [s.service for s in m.services] == ["Savings Plans for AWS Compute usage"]
    assert_reconciles(m)


def test_reconciliation_holds_for_random_lines():
    rng = random.Random(20261015)
    names = ["Usage", "Tax", "Credit", "Refund", "Support fee", "Savings Plan negation", "EdpDiscount",
             "Reservation applied usage", "Unknown A", "unknown_b"]
    groups = [g(f"svc{rng.randint(1, 6)}", rng.choice(names), f"{rng.uniform(-50, 50):.10f}") for _ in range(200)]
    # Grouping returns one line per (service, record type); merge duplicates the way AWS would.
    merged = {}
    for grp in groups:
        key = tuple(grp["Keys"])
        merged[key] = merged.get(key, Decimal(0)) + Decimal(grp["Metrics"]["UnblendedCost"]["Amount"])
    report = run({"ResultsByTime": [month(*SEP, [g(s, r, str(a)) for (s, r), a in merged.items()])]})
    m = report.month
    assert_reconciles(m)
    assert m.calculated_net == sum(merged.values(), Decimal(0))
    assert sorted(m.unclassified_record_types) == sorted({r for _, r in merged if r in ("Unknown A", "unknown_b")})


def test_credit_only_month_has_zero_charges_not_no_data():
    report = run({"ResultsByTime": [month(*SEP, [g("AWS Lambda", "Credit", "-0.40")])]})
    assert report.status == BillStatus.OK
    m = report.month
    assert m.charges_before_discounts_credits_and_refunds == 0 and m.calculated_net == Decimal("-0.40")
    assert m.services == [] and report.warnings == []


# ---------- parsing, pagination, transparency ----------

def test_pagination_merges_groups_and_counts_requests():
    report = run(
        {"ResultsByTime": [month(*SEP, [g("AWS Lambda", "Usage", "1.10")])], "NextPageToken": "t1"},
        {"ResultsByTime": [month(*SEP, [g("AWS Lambda", "Usage", "0.40"), g("Amazon S3", "Usage", "2")], estimated=True)]},
    )
    assert report.month.charges_before_discounts_credits_and_refunds == Decimal("3.50")
    assert report.month.estimated and report.warnings == [ReportWarning.ESTIMATED]
    assert report.aws_requests == 2 and report.retrieved_at == RETRIEVED and report.generated_at == NOW
    assert report.schema_version == "1" and report.cache is None


def test_page_limit_is_configurable_and_refuses_partial_results():
    pages = [{"ResultsByTime": [month(*SEP, [g("A", "Usage", "1")])], "NextPageToken": f"t{i + 1}"} for i in range(2)]
    report = run(*pages, max_pages=2)
    assert report.status == BillStatus.ERROR and report.error.code == "PageLimitExceeded" and report.month is None
    assert report.aws_requests == 2


def test_page_limit_from_environment(monkeypatch):
    monkeypatch.setenv(MAX_PAGES_ENV, "7")
    assert resolve_max_pages() == 7
    assert resolve_max_pages(3) == 3
    monkeypatch.delenv(MAX_PAGES_ENV)
    assert resolve_max_pages() == 5


@pytest.mark.parametrize("value", [0, 21])
def test_page_limit_is_bounded(value):
    with pytest.raises(ValueError):
        resolve_max_pages(value)


def test_bad_page_limit_environment_is_rejected(monkeypatch):
    monkeypatch.setenv(MAX_PAGES_ENV, "lots")
    with pytest.raises(ValueError):
        resolve_max_pages()


def test_non_usd_currency_is_reported_as_is():
    report = run({"ResultsByTime": [month(*SEP, [g("AWS Lambda", "Usage", "10", unit="EUR")])]})
    assert report.currency == "EUR" and "10.00 EUR" in bill_speech(report)


def test_mixed_currencies_are_never_summed():
    report = run({"ResultsByTime": [month(*AUG, [g("A", "Usage", "1", unit="EUR")]), month(*SEP, [g("A", "Usage", "1")])]})
    assert report.status == BillStatus.MIXED_CURRENCY and report.month is None


def test_estimated_month_is_flagged_and_spoken():
    report = run({"ResultsByTime": [month(*AUG, [g("A", "Usage", "2")]), month(*SEP, [g("A", "Usage", "1")], estimated=True)]})
    assert report.comparison.estimated and ReportWarning.ESTIMATED in report.warnings
    speech = bill_speech(report)
    assert "That's 1.00 less than August, down 50.0 percent." in speech
    assert speech.endswith("AWS still marks these figures as estimated, so they may change.")
    assert "CloudGuard calculates" not in speech  # no adjustments: the net equals the charges


def test_no_comparison_when_previous_month_has_no_lines():
    report = run({"ResultsByTime": [month(*AUG, []), month(*SEP, [g("A", "Usage", "1")])]})
    assert report.comparison is None and "than" not in bill_speech(report)


def test_previous_month_zero_gives_no_percentage():
    report = run({"ResultsByTime": [month(*AUG, [g("A", "Usage", "0")]), month(*SEP, [g("A", "Usage", "3")])]})
    assert report.comparison.change == 3 and report.comparison.change_percent is None
    assert "That's 3.00 more than August." in bill_speech(report)


# ---------- empty and failure cases ----------

@pytest.mark.parametrize("results", [[], [month(*AUG, [g("A", "Usage", "1")])], [month(*SEP, [])]])
def test_empty_billing_data_is_no_data_not_zero(results):
    report = run({"ResultsByTime": results})
    assert report.status == BillStatus.NO_DATA and report.month is None and report.aws_requests == 1
    assert bill_speech(report) == "AWS Cost Explorer returned no cost data for the September 2026 billing month."


def test_data_unavailable_separates_aws_statement_from_inferred_causes():
    report = run(errors=[("DataUnavailableException", 400)])
    assert report.status == BillStatus.DATA_UNAVAILABLE
    assert report.error.code == "DataUnavailableException"
    assert report.error.aws_description == "The requested data is unavailable."
    assert any("may not be enabled" in c for c in report.error.inferred_possible_causes)
    assert report.aws_requests == 1 and report.retrieved_at is None
    speech = bill_speech(report)
    assert speech.startswith("AWS reports that the requested cost data is unavailable. Possible causes include")
    assert "is disabled" not in speech


@pytest.mark.parametrize("code,status,expected,has_description", [
    ("AccessDeniedException", 400, BillStatus.ACCESS_DENIED, False),
    ("UnrecognizedClientException", 400, BillStatus.ACCESS_DENIED, False),
    ("BillExpirationException", 400, BillStatus.ERROR, True),
    ("LimitExceededException", 400, BillStatus.ERROR, True),
    ("InternalServerError", 500, BillStatus.ERROR, False),
])
def test_api_errors_are_classified_without_leaking_the_message(code, status, expected, has_description):
    report = run(errors=[(code, status)])
    assert report.status == expected and report.month is None
    assert report.error.code == code and report.error.operation == "ce:GetCostAndUsage"
    assert (report.error.aws_description is not None) == has_description
    if expected == BillStatus.ACCESS_DENIED:
        assert report.error.inferred_possible_causes
    dumped = report.model_dump_json()
    assert "123456789012" not in dumped and "arn:" not in dumped and "123456789012" not in bill_speech(report)


def test_missing_credentials_is_an_error():
    class NoCreds:
        def get_cost_and_usage(self, **_):
            raise NoCredentialsError()
    report = get_last_month_bill(NoCreds(), NOW)
    assert report.status == BillStatus.ERROR and report.error.code == "NoCredentialsError"
