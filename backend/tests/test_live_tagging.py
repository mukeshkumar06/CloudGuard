"""Live tagging (Checkpoint 5): per-service tagging APIs, and the approved-execution workflow against them.

Two kinds of fake AWS, never the real one:
- botocore Stubber on real clients, to prove each service gets the right API and request shape (validated against
  the AWS service models);
- FakeAws, an in-memory account behind the same client methods, to run the whole live workflow (STS identities,
  allow-list, proposal, approval, LiveRemediationExecutor) with fault injection and a log of every write.
No test calls AWS.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from authz_helpers import fake_user_id, seeded_authz
from cloudguard.governance.custom_tagging import CustomTaggingEngine
from cloudguard.governance.custom_tagging.models import (
    NameFilter, TaggingOperation, TaggingRequest, TargetCriteria,
)
from cloudguard.governance.discovery import FixtureSource
from cloudguard.governance.policy_store import LocalPolicyStore, PolicyManager
from cloudguard.governance.remediation.approval import DecisionNotAllowedError, DecisionService, new_nonce
from cloudguard.governance.remediation.execution_models import ResultStatus
from cloudguard.governance.remediation.executor import (
    ExecutionNotAllowedError, FixtureRemediationExecutor, LiveExecutionNotSupportedError, LiveRemediationExecutor,
)
from cloudguard.governance.remediation.identity import DevelopmentIdentityProvider, StsCallerIdentityProvider
from cloudguard.governance.remediation.live_writer import (
    LiveAccessDenied, LiveTagAccount, LiveThrottled, LiveUnsupported, LiveWriter, parse_arn,
)
from cloudguard.governance.remediation.models import ProposalState
from cloudguard.governance.remediation.repository import LocalProposalRepository, ProposalManager
from cloudguard.governance.remediation.writers import FixtureAccount, ReadPurpose

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "backend" / "fixtures" / "custom-tagging-demo.json"
ACCT = "123456789012"
T0 = datetime(2026, 10, 14, 6, 30, tzinfo=UTC)
LAMBDA = f"arn:aws:lambda:ap-south-1:{ACCT}:function:image-resizer"
TABLE = f"arn:aws:dynamodb:ap-south-1:{ACCT}:table/orders"
BUCKET = "arn:aws:s3:::acme-logs"
INSTANCE = f"arn:aws:ec2:ap-south-1:{ACCT}:instance/i-0123456789abcdef0"
ROLE_REQ, ROLE_APP, ROLE_EXE = (f"arn:aws:iam::{ACCT}:role/{r}" for r in ("Requester", "Approver", "Executor"))
# The requester may approve too, so that separation of duties (not a missing permission) is what refuses it.
LIVE_AUTHZ = {"accounts": {ACCT: {"create": [ROLE_REQ], "approve": [ROLE_APP, ROLE_REQ], "reject": [ROLE_APP],
                                  "execute": [ROLE_EXE, ROLE_APP]}}}


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def client(service, region="ap-south-1"):
    return boto3.client(service, region_name=region, aws_access_key_id="t", aws_secret_access_key="t")


def meta(request_id="req-1"):
    return {"ResponseMetadata": {"RequestId": request_id, "HTTPStatusCode": 200}}


def stubbed_account(clients):
    return LiveTagAccount(session=None, account_id=ACCT, client_factory=lambda service, region: clients[service])


# ================================================================ request shapes (Stubber)

def test_lambda_uses_list_tags_and_tag_resource_with_a_tag_map():
    c = client("lambda")
    with Stubber(c) as st:
        st.add_response("list_tags", {"Tags": {"Owner": "a"}}, {"Resource": LAMBDA})
        st.add_response("tag_resource", {}, {"Resource": LAMBDA, "Tags": {"Project": "CloudGuard", "Team": "x"}})
        account = stubbed_account({"lambda": c})
        snap = account.read(LAMBDA, ReadPurpose.PREWRITE)
        written = account._apply_tags(LAMBDA, {"Project": "CloudGuard", "Team": "x"})
        st.assert_no_pending_responses()
    assert snap.exists and snap.tags == {"Owner": "a"} and snap.uid is None
    assert written.written_keys == ["Project", "Team"]


def test_dynamodb_uses_tag_resource_with_a_key_value_list_and_reads_the_table_id():
    c = client("dynamodb")
    with Stubber(c) as st:
        st.add_response("describe_table", {"Table": {"TableName": "orders", "TableId": "uuid-1"}}, {"TableName": "orders"})
        st.add_response("list_tags_of_resource", {"Tags": [{"Key": "Environment", "Value": "prod"}], "NextToken": "n"},
                        {"ResourceArn": TABLE})
        st.add_response("list_tags_of_resource", {"Tags": [{"Key": "Project", "Value": "checkout"}]},
                        {"ResourceArn": TABLE, "NextToken": "n"})
        st.add_response("tag_resource", {}, {"ResourceArn": TABLE, "Tags": [{"Key": "Team", "Value": "data"}]})
        account = stubbed_account({"dynamodb": c})
        snap = account.read(TABLE, ReadPurpose.PREWRITE)
        account._apply_tags(TABLE, {"Team": "data"})
        st.assert_no_pending_responses()
    assert snap.uid == "uuid-1" and snap.tags == {"Environment": "prod", "Project": "checkout"}  # paginated


def test_ec2_uses_create_tags_on_the_instance_id():
    c = client("ec2")
    with Stubber(c) as st:
        st.add_response("describe_instances", {"Reservations": [{"Instances": [{
            "InstanceId": "i-0123456789abcdef0", "State": {"Name": "running"},
            "Tags": [{"Key": "Name", "Value": "build-agent"}]}]}]}, {"InstanceIds": ["i-0123456789abcdef0"]})
        st.add_response("create_tags", {}, {"Resources": ["i-0123456789abcdef0"],
                                             "Tags": [{"Key": "Team", "Value": "platform"}]})
        account = stubbed_account({"ec2": c})
        snap = account.read(INSTANCE, ReadPurpose.PREWRITE)
        account._apply_tags(INSTANCE, {"Team": "platform"})
        st.assert_no_pending_responses()
    assert snap.uid == "i-0123456789abcdef0" and snap.tags == {"Name": "build-agent"}


def test_s3_merges_with_the_current_tag_set_because_put_bucket_tagging_replaces_it():
    c = client("s3", "us-east-1")
    with Stubber(c) as st:
        st.add_response("get_bucket_location", {"LocationConstraint": "ap-south-1"}, {"Bucket": "acme-logs"})
        st.add_response("get_bucket_tagging", {"TagSet": [{"Key": "project", "Value": "legacy"}]}, {"Bucket": "acme-logs"})
        st.add_response("put_bucket_tagging", {}, {"Bucket": "acme-logs", "Tagging": {"TagSet": [
            {"Key": "Team", "Value": "web"}, {"Key": "project", "Value": "legacy"}]}})
        stubbed_account({"s3": c})._apply_tags(BUCKET, {"Team": "web"})
        st.assert_no_pending_responses()  # the existing 'project' tag was written back, not dropped


def test_s3_read_resolves_region_creation_date_and_an_empty_tag_set():
    c = client("s3", "us-east-1")
    created = datetime(2025, 1, 2, tzinfo=UTC)
    with Stubber(c) as st:
        st.add_response("get_bucket_location", {"LocationConstraint": "EU"}, {"Bucket": "acme-logs"})
        st.add_response("list_buckets", {"Buckets": [{"Name": "acme-logs", "CreationDate": created}]},
                        {"Prefix": "acme-logs"})
        st.add_client_error("get_bucket_tagging", "NoSuchTagSet", "none", 404)
        snap = stubbed_account({"s3": c}).read(BUCKET, ReadPurpose.VERIFY)
    assert snap.exists and snap.region == "eu-west-1" and snap.tags == {} and snap.uid == created.isoformat()


@pytest.mark.parametrize("code,error", [("ThrottlingException", LiveThrottled), ("AccessDeniedException", LiveAccessDenied)])
def test_aws_errors_map_onto_the_executor_categories(code, error):
    c = client("lambda")
    with Stubber(c) as st:
        st.add_client_error("tag_resource", code, "secret message naming arn:aws:iam::123456789012:user/x", 400)
        with pytest.raises(error) as info:
            stubbed_account({"lambda": c})._apply_tags(LAMBDA, {"Team": "x"})
    assert "user/x" not in str(info.value)  # AWS message text (which can name the caller) is never kept


def test_a_missing_resource_reads_as_not_existing():
    c = client("lambda")
    with Stubber(c) as st:
        st.add_client_error("list_tags", "ResourceNotFoundException", "gone", 404)
        assert stubbed_account({"lambda": c}).read(LAMBDA, ReadPurpose.REVALIDATE).exists is False


def test_unsupported_arns_and_writers_are_refused():
    with pytest.raises(LiveUnsupported):
        parse_arn(f"arn:aws:iam::{ACCT}:role/admin")
    with pytest.raises(TypeError):
        LiveWriter(FixtureAccount(ACCT, ["ap-south-1"]))
    assert parse_arn(BUCKET) == ("AWS::S3::Bucket", "", "", "acme-logs")


# ================================================================ the live workflow (FakeAws)

def _err(code, status=400):
    return ClientError({"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}}, "op")


class FakeAws:
    """An in-memory account answering the same client calls LiveTagAccount makes. `writes` logs every write."""

    def __init__(self):
        d = FixtureSource(FIXTURE).discover()
        self.res = {r.resource_arn: {"type": r.resource_type, "region": r.region, "name": r.resource_id,
                                     "tags": dict(r.tags) if r.tags is not None else None, "uid": f"uid-{r.resource_id}"}
                    for r in d.resources}
        self.writes: list[tuple[str, dict]] = []
        self.fail_write: dict[str, list[str]] = {}  # arn -> error codes to raise, in order
        self.silent: set[str] = set()  # writes that report success but change nothing

    def factory(self, service, region):
        return _FakeClient(self, service)

    def write(self, arn, tags):
        if self.fail_write.get(arn):
            raise _err(self.fail_write[arn].pop(0))
        self.writes.append((arn, dict(tags)))
        if arn not in self.silent:
            self.res[arn]["tags"].update(tags)
        return meta("w")


class _FakeClient:
    def __init__(self, aws, service):
        self.aws, self.service = aws, service

    def _by_name(self, kind, name):
        return next((a for a, r in self.aws.res.items() if r["type"] == kind and r["name"] == name), None)

    # lambda
    def list_tags(self, Resource):
        r = self.aws.res.get(Resource)
        if r is None:
            raise _err("ResourceNotFoundException", 404)
        return {"Tags": dict(r["tags"]), **meta()}

    def tag_resource(self, **kw):
        if self.service == "lambda":
            return self.aws.write(kw["Resource"], kw["Tags"])
        return self.aws.write(kw["ResourceArn"], {t["Key"]: t["Value"] for t in kw["Tags"]})

    # dynamodb
    def describe_table(self, TableName):
        arn = self._by_name("AWS::DynamoDB::Table", TableName)
        if arn is None:
            raise _err("ResourceNotFoundException", 400)
        return {"Table": {"TableName": TableName, "TableId": self.aws.res[arn]["uid"]}}

    def list_tags_of_resource(self, ResourceArn):
        return {"Tags": [{"Key": k, "Value": v} for k, v in self.aws.res[ResourceArn]["tags"].items()], **meta()}

    # ec2
    def describe_instances(self, InstanceIds):
        arn = self._by_name("AWS::EC2::Instance", InstanceIds[0])
        if arn is None:
            raise _err("InvalidInstanceID.NotFound")
        r = self.aws.res[arn]
        return {"Reservations": [{"Instances": [{"InstanceId": r["uid"], "State": {"Name": "running"},
                                                 "Tags": [{"Key": k, "Value": v} for k, v in r["tags"].items()]}]}], **meta()}

    def create_tags(self, Resources, Tags):
        return self.aws.write(self._by_name("AWS::EC2::Instance", Resources[0]), {t["Key"]: t["Value"] for t in Tags})

    # s3
    def get_bucket_location(self, Bucket):
        return {"LocationConstraint": self.aws.res[f"arn:aws:s3:::{Bucket}"]["region"]}

    def list_buckets(self, Prefix):
        arn = f"arn:aws:s3:::{Prefix}"
        return {"Buckets": [{"Name": Prefix, "CreationDate": self.aws.res[arn]["uid"]}] if arn in self.aws.res else [],
                **meta()}

    def get_bucket_tagging(self, Bucket):
        tags = self.aws.res[f"arn:aws:s3:::{Bucket}"]["tags"]
        if tags is None:
            raise _err("AccessDenied", 403)
        return {"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()], **meta()}

    def put_bucket_tagging(self, Bucket, Tagging):
        arn = f"arn:aws:s3:::{Bucket}"
        new = {t["Key"]: t["Value"] for t in Tagging["TagSet"]}
        if self.aws.fail_write.get(arn):
            raise _err(self.aws.fail_write[arn].pop(0))
        self.aws.writes.append((arn, dict(new)))
        if arn not in self.aws.silent:
            self.aws.res[arn]["tags"] = new  # replaces the whole set, like S3
        return meta("w")


class Clock:
    def __init__(self):
        self.now = T0 + timedelta(minutes=1)

    def __call__(self):
        return self.now


class FakeSts:
    def __init__(self, arn):
        self.arn = arn

    def get_caller_identity(self):
        return {"Arn": self.arn, "Account": ACCT, "UserId": fake_user_id(self.arn, ACCT)}


def assumed(role):
    return f"arn:aws:sts::{ACCT}:assumed-role/{role}/s1"


class LiveWorld:
    """Proposal store + live allow-list + STS identities + FakeAws, around one proposal."""

    def __init__(self, request: TaggingRequest, ttl=timedelta(minutes=15)):
        self.clock = Clock()
        self.aws = FakeAws()
        self.account = LiveTagAccount(session=None, account_id=ACCT, client_factory=self.aws.factory)
        self.policies = PolicyManager(LocalPolicyStore())
        self.policies.ensure_default()
        self.proposals = ProposalManager(LocalProposalRepository(), clock=self.clock, policy_manager=self.policies)
        self.authz = seeded_authz(LIVE_AUTHZ)
        outcome = CustomTaggingEngine(FixtureSource(FIXTURE), self.policies.get_active(), clock=lambda: T0,
                                      proposal_ttl=ttl).prepare_request(request)
        self.proposal = outcome.proposal
        changing = [r.resource_arn for r in self.proposal.records if r.changes]
        self.svc("Requester").create_proposal(self.proposal, idempotency_key="key-create-live1",
                                              resource_identities=self.account.identities(changing))
        self.writes_after_create = list(self.aws.writes)

    def svc(self, role):
        return DecisionService(self.proposals, self.authz, StsCallerIdentityProvider(FakeSts(assumed(role)),
                                                                                     clock=self.clock), clock=self.clock)

    def approve(self, role="Approver", **kw):
        return self.svc(role).approve(self.proposal.proposal_id, content_hash=kw.pop("content_hash",
                                      self.proposal.content_hash), nonce=new_nonce(),
                                      idempotency_key=kw.pop("idempotency_key", "key-approve-live1"),
                                      acknowledge_overwrites=kw.pop("acknowledge_overwrites",
                                                                    self.proposal.requires_overwrite_confirmation)).decision

    def execute(self, approval_id, role="Executor", key="key-exec-live001"):
        return LiveRemediationExecutor(self.svc(role), self.account, clock=self.clock).execute(
            self.proposal.proposal_id, approval_id=approval_id, idempotency_key=key)


def selection(*arns, tags=None, op=TaggingOperation.BULK_ADD_TAG):
    return TaggingRequest(operation=op, target=TargetCriteria(all_of_type=True, arns=tuple(arns)),
                          tags=tags or {"Team": "platform", "CostCenter": "cc-42"}, raw_text="test", parsed_by="test")


ALL_FOUR = (LAMBDA, TABLE, BUCKET, INSTANCE)


def test_approved_tags_are_written_to_all_four_types_and_verified():
    w = LiveWorld(selection(*ALL_FOUR))
    view = w.execute(w.approve().decision_id)
    assert view.summary.outcome == ProposalState.SUCCEEDED
    assert {a: r.status for a, r in view.results.items()} == {a: ResultStatus.APPLIED_VERIFIED for a in ALL_FOUR}
    for arn in ALL_FOUR:
        assert w.aws.res[arn]["tags"]["Team"] == "platform" and w.aws.res[arn]["tags"]["CostCenter"] == "cc-42"
    # Existing tags are kept (S3's write replaced the set with a merged one).
    assert w.aws.res[BUCKET]["tags"]["project"] == "legacy" and w.aws.res[TABLE]["tags"]["Project"] == "checkout"
    assert {a for a, _ in w.aws.writes} == set(ALL_FOUR)


def test_zero_aws_writes_before_approval_and_execution_needs_an_approval():
    w = LiveWorld(selection(*ALL_FOUR))
    assert w.aws.writes == [] and w.writes_after_create == []  # preview, save and identity capture only read
    with pytest.raises(ExecutionNotAllowedError):
        w.execute("approval-that-does-not-exist")
    assert w.aws.writes == []


def test_the_approver_cannot_execute_and_the_requester_cannot_approve():
    w = LiveWorld(selection(LAMBDA))
    with pytest.raises(DecisionNotAllowedError, match="separation of duties"):
        w.approve(role="Requester")
    approval = w.approve()
    with pytest.raises(ExecutionNotAllowedError, match="separation of duties"):
        w.execute(approval.decision_id, role="Approver")
    assert w.aws.writes == []


def test_an_approval_binds_the_exact_content_hash():
    w = LiveWorld(selection(LAMBDA))
    with pytest.raises(Exception, match="hash"):
        w.approve(content_hash="sha256:" + "0" * 64)
    assert w.aws.writes == []


def test_an_approval_is_used_once():
    w = LiveWorld(selection(LAMBDA, TABLE))
    approval = w.approve()
    first = w.execute(approval.decision_id)
    assert first.summary.outcome == ProposalState.SUCCEEDED
    writes = len(w.aws.writes)
    assert w.execute(approval.decision_id).record.execution_id == first.record.execution_id  # same request: replay
    with pytest.raises(ExecutionNotAllowedError, match="terminal"):
        w.execute(approval.decision_id, key="key-exec-live002")  # a new request with the old approval
    assert len(w.aws.writes) == writes


def test_an_expired_approval_never_executes():
    w = LiveWorld(selection(LAMBDA))
    approval = w.approve()
    w.clock.now += timedelta(hours=2)
    with pytest.raises(ExecutionNotAllowedError):
        w.execute(approval.decision_id)
    assert w.aws.writes == []


def test_partial_failure_is_reported_per_resource():
    w = LiveWorld(selection(LAMBDA, TABLE, INSTANCE))
    w.aws.fail_write[TABLE] = ["AccessDeniedException"]
    view = w.execute(w.approve().decision_id)
    assert view.summary.outcome == ProposalState.PARTIAL
    assert view.results[TABLE].status == ResultStatus.FAILED_WRITE
    assert view.results[TABLE].error_category.value == "ACCESS_DENIED"
    assert view.results[LAMBDA].status == view.results[INSTANCE].status == ResultStatus.APPLIED_VERIFIED


def test_throttling_is_retried_within_the_write_budget():
    w = LiveWorld(selection(LAMBDA))
    w.aws.fail_write[LAMBDA] = ["ThrottlingException"]
    view = w.execute(w.approve().decision_id)
    assert view.results[LAMBDA].status == ResultStatus.APPLIED_VERIFIED and view.results[LAMBDA].attempts == 2


def test_read_after_write_catches_a_write_that_did_not_take_effect():
    w = LiveWorld(selection(LAMBDA))
    w.aws.silent.add(LAMBDA)
    view = w.execute(w.approve().decision_id)
    assert view.results[LAMBDA].status == ResultStatus.FAILED_VERIFICATION


def test_a_recreated_resource_is_not_tagged_by_the_old_approval():
    w = LiveWorld(selection(TABLE))
    approval = w.approve()
    w.aws.res[TABLE]["uid"] = "uid-recreated"
    view = w.execute(approval.decision_id)
    assert view.results[TABLE].status == ResultStatus.SKIPPED and w.aws.writes == []


def test_existing_values_are_never_overwritten_silently():
    # An add never overwrites: billing-api already has Owner=payments-team, so it is a conflict, not a change,
    # and a proposal that changes nothing can't even be stored.
    add = CustomTaggingEngine(FixtureSource(FIXTURE), _policy()).prepare_request(TaggingRequest(
        operation=TaggingOperation.BULK_ADD_TAG,
        target=TargetCriteria(resource_type="AWS::Lambda::Function", all_of_type=True,
                              name_filter=NameFilter(match="contains", text="billing")),
        tags={"Owner": "backend"}, raw_text="t", parsed_by="test"))
    assert not add.proposal.is_approvable and add.proposal.impact.resources_conflicting == 1
    # An update is explicit, and its approval must acknowledge the overwrite.
    w = LiveWorld(TaggingRequest(operation=TaggingOperation.BULK_UPDATE_TAG,
                                 target=TargetCriteria(resource_type="AWS::Lambda::Function", all_of_type=True,
                                                       name_filter=NameFilter(match="contains", text="billing")),
                                 tags={"Owner": "backend"}, raw_text="t", parsed_by="test"))
    assert w.proposal.requires_overwrite_confirmation
    with pytest.raises(Exception, match="overwrit"):
        w.approve(acknowledge_overwrites=False)
    view = w.execute(w.approve(idempotency_key="key-approve-live2").decision_id)
    arn = f"arn:aws:lambda:ap-south-1:{ACCT}:function:billing-api"
    assert view.results[arn].status == ResultStatus.APPLIED_VERIFIED and w.aws.res[arn]["tags"]["Owner"] == "backend"
    assert w.aws.res[arn]["tags"]["Project"] == "OtherProject"


def test_name_filters_select_matching_resources_only():
    w = LiveWorld(TaggingRequest(operation=TaggingOperation.BULK_ADD_TAG,
                                 target=TargetCriteria(all_of_type=True, name_filter=NameFilter(match="starts_with",
                                                                                                text="SHARED")),
                                 tags={"Team": "platform"}, raw_text="t", parsed_by="test"))
    assert {r.resource_name for r in w.proposal.records} == {"shared-name"}
    assert {r.resource_type for r in w.proposal.records} == {"AWS::Lambda::Function", "AWS::DynamoDB::Table"}


@pytest.mark.parametrize("key", ["aws:createdBy", "cloudguard:protected", "CloudGuard:x"])
def test_reserved_tag_namespaces_are_refused(key):
    outcome = CustomTaggingEngine(FixtureSource(FIXTURE), _policy()).prepare_request(selection(LAMBDA, tags={key: "x"}))
    assert outcome.status.value == "REJECTED" and outcome.proposal is None


def _policy():
    pm = PolicyManager(LocalPolicyStore())
    pm.ensure_default()
    return pm.get_active()


def test_live_and_fixture_executors_refuse_each_others_worlds():
    w = LiveWorld(selection(LAMBDA))
    with pytest.raises(TypeError):
        LiveRemediationExecutor(w.svc("Executor"), FixtureAccount(ACCT, ["ap-south-1"]))
    dev = DecisionService(w.proposals, w.authz, DevelopmentIdentityProvider("alice", clock=w.clock), clock=w.clock)
    with pytest.raises(ExecutionNotAllowedError):
        LiveRemediationExecutor(dev, w.account)
    with pytest.raises(LiveExecutionNotSupportedError):
        FixtureRemediationExecutor(w.svc("Executor"), w.account)
    assert w.aws.writes == []


def test_unchanged_proposal_hashes_for_targets_without_the_new_fields():
    t = TargetCriteria(resource_type="AWS::Lambda::Function", all_of_type=True)
    assert json.loads(t.model_dump_json()) == {"resource_type": "AWS::Lambda::Function", "name": None, "arn": None,
                                               "account_id": None, "region": None, "all_of_type": True}


def test_all_lambdas_proposal_executes_on_every_function_and_nothing_else():
    w = LiveWorld(TaggingRequest(operation=TaggingOperation.BULK_ADD_TAG,
                                 target=TargetCriteria(resource_type="AWS::Lambda::Function", all_of_type=True),
                                 tags={"Team": "platform"}, raw_text="Tag all Lambda functions with Team=platform",
                                 parsed_by="test"))
    lambdas = {a for a, r in w.aws.res.items() if r["type"] == "AWS::Lambda::Function"}
    assert {r.resource_arn for r in w.proposal.records} == lambdas  # both 'worker' functions, in both regions
    view = w.execute(w.approve().decision_id)
    assert view.summary.outcome == ProposalState.SUCCEEDED
    assert {a for a, _ in w.aws.writes} == lambdas
    assert all(w.aws.res[a]["tags"]["Team"] == "platform" for a in lambdas)
    assert all(r.status == ResultStatus.APPLIED_VERIFIED for r in view.results.values())
