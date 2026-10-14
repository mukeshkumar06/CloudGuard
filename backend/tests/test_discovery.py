"""Discovery tests against botocore Stubber: real request/response shapes, no network, no credentials."""

from datetime import UTC, datetime

import boto3
import pytest
from botocore.stub import Stubber

from cloudguard.governance.discovery.common import classify_error
from cloudguard.governance.discovery.dynamodb_discovery import discover_dynamodb
from cloudguard.governance.discovery.ec2_discovery import discover_ec2
from cloudguard.governance.discovery.lambda_discovery import discover_lambda
from cloudguard.governance.discovery.s3_discovery import discover_s3
from cloudguard.governance.models import ErrorKind

NOW = datetime(2026, 10, 14, tzinfo=UTC)
REGION = "ap-south-1"
ACCOUNT = "123456789012"


def client(service):
    return boto3.client(
        service, region_name=REGION, aws_access_key_id="testing", aws_secret_access_key="testing"
    )


def fn(name):
    return {"FunctionName": name, "FunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}"}


# ---------- Lambda ----------

def test_lambda_paginates_and_reads_tags():
    c = client("lambda")
    with Stubber(c) as st:
        st.add_response("list_functions", {"Functions": [fn("a")], "NextMarker": "m1"}, {})
        st.add_response("list_tags", {"Tags": {"Owner": "eng"}}, {"Resource": fn("a")["FunctionArn"]})
        st.add_response("list_functions", {"Functions": [fn("b")]}, {"Marker": "m1"})
        st.add_response("list_tags", {"Tags": {}}, {"Resource": fn("b")["FunctionArn"]})
        resources, errors = discover_lambda(c, REGION, ACCOUNT, NOW)
        st.assert_no_pending_responses()
    assert [r.resource_name for r in resources] == ["a", "b"]
    assert resources[0].tags == {"Owner": "eng"} and resources[1].tags == {}
    assert resources[0].resource_type == "AWS::Lambda::Function"
    assert errors == []


def test_lambda_tag_access_denied_is_per_resource_error():
    c = client("lambda")
    with Stubber(c) as st:
        st.add_response("list_functions", {"Functions": [fn("a"), fn("b")]}, {})
        st.add_client_error("list_tags", "AccessDeniedException", "denied", 403)
        st.add_response("list_tags", {"Tags": {"Owner": "eng"}})
        resources, errors = discover_lambda(c, REGION, ACCOUNT, NOW)
    assert resources[0].tags is None
    assert resources[0].tag_error.kind == ErrorKind.ACCESS_DENIED
    assert resources[0].tag_error.operation == "lambda:ListTags"
    assert resources[1].tags == {"Owner": "eng"}
    assert errors == []


def test_lambda_list_failure_is_service_error():
    c = client("lambda")
    with Stubber(c) as st:
        st.add_client_error("list_functions", "AccessDeniedException", "denied", 403)
        resources, errors = discover_lambda(c, REGION, ACCOUNT, NOW)
    assert resources == []
    assert errors[0].service == "lambda" and errors[0].error.operation == "lambda:ListFunctions"


# ---------- DynamoDB ----------

def table_arn(name):
    return f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{name}"


def test_dynamodb_paginates_tables_and_tags():
    c = client("dynamodb")
    with Stubber(c) as st:
        st.add_response("list_tables", {"TableNames": ["orders"], "LastEvaluatedTableName": "orders"}, {})
        st.add_response("describe_table", {"Table": {"TableName": "orders", "TableArn": table_arn("orders")}}, {"TableName": "orders"})
        st.add_response("list_tags_of_resource", {"Tags": [{"Key": "Project", "Value": "x"}], "NextToken": "t1"},
                        {"ResourceArn": table_arn("orders")})
        st.add_response("list_tags_of_resource", {"Tags": [{"Key": "Owner", "Value": "eng"}]},
                        {"ResourceArn": table_arn("orders"), "NextToken": "t1"})
        st.add_response("list_tables", {"TableNames": ["sample-sessions"]}, {"ExclusiveStartTableName": "orders"})
        st.add_response("describe_table", {"Table": {"TableName": "sample-sessions", "TableArn": table_arn("sample-sessions")}}, {"TableName": "sample-sessions"})
        st.add_response("list_tags_of_resource", {"Tags": []}, {"ResourceArn": table_arn("sample-sessions")})
        resources, errors = discover_dynamodb(c, REGION, ACCOUNT, "aws", NOW)
        st.assert_no_pending_responses()
    assert resources[0].tags == {"Project": "x", "Owner": "eng"}
    assert resources[1].tags == {} and resources[1].resource_arn == table_arn("sample-sessions")
    assert errors == []


def test_dynamodb_throttled_tag_call_is_inspection_error():
    c = client("dynamodb")
    with Stubber(c) as st:
        st.add_response("list_tables", {"TableNames": ["orders"]})
        st.add_response("describe_table", {"Table": {"TableName": "orders", "TableArn": table_arn("orders")}})
        st.add_client_error("list_tags_of_resource", "ThrottlingException", "slow down", 400)
        resources, _ = discover_dynamodb(c, REGION, ACCOUNT, "aws", NOW)
    assert resources[0].tag_error.kind == ErrorKind.THROTTLED
    assert resources[0].tag_error.operation == "dynamodb:ListTagsOfResource"


def test_dynamodb_describe_failure_still_reports_table():
    c = client("dynamodb")
    with Stubber(c) as st:
        st.add_response("list_tables", {"TableNames": ["orders"]})
        st.add_client_error("describe_table", "AccessDeniedException", "denied", 400)
        resources, _ = discover_dynamodb(c, REGION, ACCOUNT, "aws", NOW)
    assert resources[0].resource_arn == table_arn("orders")
    assert resources[0].tag_error.operation == "dynamodb:DescribeTable"


# ---------- S3 ----------

def buckets(*names, token=None):
    page = {"Buckets": [{"Name": n, "BucketRegion": REGION} for n in names], "Owner": {"ID": "o"}}
    if token:
        page["ContinuationToken"] = token
    return page


def test_s3_no_tag_set_means_empty_tags_not_failure():
    c = client("s3")
    with Stubber(c) as st:
        st.add_response("list_buckets", buckets("a", token="c1"), {"BucketRegion": REGION})
        st.add_client_error("get_bucket_tagging", "NoSuchTagSet", "The TagSet does not exist", 404)
        st.add_response("list_buckets", buckets("b"), {"BucketRegion": REGION, "ContinuationToken": "c1"})
        st.add_response("get_bucket_tagging", {"TagSet": [{"Key": "Owner", "Value": "eng"}]}, {"Bucket": "b"})
        resources, errors = discover_s3(c, REGION, ACCOUNT, "aws", NOW)
        st.assert_no_pending_responses()
    assert resources[0].tags == {} and resources[0].tag_error is None
    assert resources[0].resource_arn == "arn:aws:s3:::a"
    assert resources[1].tags == {"Owner": "eng"}
    assert errors == []


def test_s3_access_denied_is_inspection_error():
    c = client("s3")
    with Stubber(c) as st:
        st.add_response("list_buckets", buckets("locked"))
        st.add_client_error("get_bucket_tagging", "AccessDenied", "denied", 403)
        resources, _ = discover_s3(c, REGION, ACCOUNT, "aws", NOW)
    assert resources[0].tags is None and resources[0].tag_error.kind == ErrorKind.ACCESS_DENIED


# ---------- EC2 ----------

def test_ec2_reads_inline_tags_name_and_filters_live_states():
    c = client("ec2")
    expected = {"Filters": [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]}
    page = {
        "Reservations": [
            {
                "OwnerId": ACCOUNT,
                "Instances": [
                    {"InstanceId": "i-1", "Tags": [{"Key": "Name", "Value": "web"}, {"Key": "Owner", "Value": ""}]},
                    {"InstanceId": "i-2"},
                ],
            }
        ]
    }
    with Stubber(c) as st:
        st.add_response("describe_instances", page, expected)
        resources, errors = discover_ec2(c, REGION, ACCOUNT, "aws", NOW)
    assert [(r.resource_id, r.resource_name) for r in resources] == [("i-1", "web"), ("i-2", "i-2")]
    assert resources[0].tags == {"Name": "web", "Owner": ""}
    assert resources[1].tags == {}
    assert resources[0].resource_arn == f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-1"
    assert errors == []


def test_ec2_unauthorized_is_service_error():
    c = client("ec2")
    with Stubber(c) as st:
        st.add_client_error("describe_instances", "UnauthorizedOperation", "no", 403)
        resources, errors = discover_ec2(c, REGION, ACCOUNT, "aws", NOW)
    assert resources == [] and errors[0].error.kind == ErrorKind.ACCESS_DENIED


# ---------- error classification ----------

@pytest.mark.parametrize(
    ("code", "status", "kind"),
    [
        ("AccessDenied", 403, ErrorKind.ACCESS_DENIED),
        ("RequestLimitExceeded", 400, ErrorKind.THROTTLED),
        ("ResourceNotFoundException", 400, ErrorKind.NOT_FOUND),
        ("SomethingOdd", 503, ErrorKind.SERVICE_UNAVAILABLE),
        ("SomethingOdd", 400, ErrorKind.UNKNOWN),
    ],
)
def test_classify_error(code, status, kind):
    from botocore.exceptions import ClientError

    exc = ClientError({"Error": {"Code": code, "Message": "m"}, "ResponseMetadata": {"HTTPStatusCode": status}}, "Op")
    assert classify_error(exc, "svc:Op").kind == kind


def test_s3_falls_back_to_get_bucket_location_and_skips_other_regions():
    c = client("s3")
    page = {"Buckets": [{"Name": "here"}, {"Name": "elsewhere"}, {"Name": "virginia"}], "Owner": {"ID": "o"}}
    with Stubber(c) as st:
        st.add_response("list_buckets", page, {"BucketRegion": REGION})
        st.add_response("get_bucket_location", {"LocationConstraint": REGION}, {"Bucket": "here"})
        st.add_response("get_bucket_tagging", {"TagSet": []}, {"Bucket": "here"})
        st.add_response("get_bucket_location", {"LocationConstraint": "eu-west-1"}, {"Bucket": "elsewhere"})
        st.add_response("get_bucket_location", {}, {"Bucket": "virginia"})  # None => us-east-1
        resources, _ = discover_s3(c, REGION, ACCOUNT, "aws", NOW)
        st.assert_no_pending_responses()
    assert [(r.resource_name, r.region) for r in resources] == [("here", REGION)]


def test_lambda_deleted_between_list_and_tags_is_reported_not_dropped():
    c = client("lambda")
    with Stubber(c) as st:
        st.add_response("list_functions", {"Functions": [fn("gone")]})
        st.add_client_error("list_tags", "ResourceNotFoundException", "Function not found", 404)
        resources, _ = discover_lambda(c, REGION, ACCOUNT, NOW)
    assert resources[0].tag_error.kind == ErrorKind.NOT_FOUND


# ---------- LiveSource orchestration ----------

class FakeSession:
    """Hands out stubbed clients so LiveSource can be tested end to end without AWS."""

    def __init__(self):
        self.clients = {}

    def client(self, service, region_name, config=None):
        key = (service, region_name)
        if key not in self.clients:
            self.clients[key] = boto3.client(
                service, region_name=region_name, aws_access_key_id="t", aws_secret_access_key="t"
            )
        return self.clients[key]

    def get_partition_for_region(self, region):
        return "aws"


def test_live_source_uses_sts_account_and_scans_each_region():
    from cloudguard.governance.discovery import LiveSource

    session = FakeSession()
    stubbers = []
    sts = Stubber(session.client("sts", "ap-south-1"))
    sts.add_response("get_caller_identity", {"Account": "999988887777", "Arn": "arn:aws:iam::999988887777:user/u", "UserId": "u"})
    stubbers.append(sts)
    for region in ("ap-south-1", "us-east-1"):
        lam = Stubber(session.client("lambda", region))
        lam.add_response("list_functions", {"Functions": [
            {"FunctionName": f"f-{region}", "FunctionArn": f"arn:aws:lambda:{region}:999988887777:function:f-{region}"}]})
        lam.add_response("list_tags", {"Tags": {}})
        ddb = Stubber(session.client("dynamodb", region))
        ddb.add_response("list_tables", {"TableNames": []})
        s3 = Stubber(session.client("s3", region))
        s3.add_response("list_buckets", {"Buckets": [], "Owner": {"ID": "o"}}, {"BucketRegion": region})
        ec2 = Stubber(session.client("ec2", region))
        ec2.add_response("describe_instances", {"Reservations": []})
        stubbers += [lam, ddb, s3, ec2]
    for s in stubbers:
        s.activate()
    try:
        result = LiveSource(session, ["ap-south-1", "us-east-1", "ap-south-1"]).discover()
    finally:
        for s in stubbers:
            s.deactivate()
    for s in stubbers:
        s.assert_no_pending_responses()
    assert result.account_id == "999988887777"
    assert result.regions == ["ap-south-1", "us-east-1"]
    assert [(r.resource_name, r.region, r.account_id) for r in result.resources] == [
        ("f-ap-south-1", "ap-south-1", "999988887777"),
        ("f-us-east-1", "us-east-1", "999988887777"),
    ]


def test_unreachable_service_endpoint_is_service_error():
    from botocore.exceptions import EndpointConnectionError

    c = client("ec2")

    def boom(**_):
        raise EndpointConnectionError(endpoint_url="https://ec2.example")
    c.get_paginator = lambda _name: type("P", (), {"paginate": staticmethod(boom)})()
    resources, errors = discover_ec2(c, REGION, ACCOUNT, "aws", NOW)
    assert resources == [] and errors[0].error.kind == ErrorKind.SERVICE_UNAVAILABLE
