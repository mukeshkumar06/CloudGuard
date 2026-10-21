"""Checkpoint 4.3: fixture revalidation and remediation executor.

Everything runs against FixtureAccount (in memory). No AWS client is created for writes; a safety
test asserts that boto3 is never asked for a tagging-capable client during execution.
"""

import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from cloudguard.governance.__main__ import main
from cloudguard.governance.custom_tagging import CustomTaggingEngine, RuleBasedParser
from cloudguard.governance.custom_tagging.models import ChangeAction, TaggingRequest
from cloudguard.governance.discovery import FixtureSource
from cloudguard.governance.policy import DEFAULT_POLICY_DOCUMENT, parse_policy
from cloudguard.governance.policy_store import LocalPolicyStore, PolicyManager
from cloudguard.governance.remediation import executor as executor_module
from cloudguard.governance.remediation import writers as writers_module
from cloudguard.governance.remediation.approval import AuthorizationError, DecisionService, new_nonce
from cloudguard.governance.remediation.authz import BOOTSTRAP_ENV, AuthzManager, LocalAuthzStore, parse_authz
from authz_helpers import edit_authz, seeded_authz
from cloudguard.governance.remediation.execution_models import (
    ApprovedChange,
    ErrorCategory,
    ExecutionIntegrityError,
    ResultStatus,
    RevalidationStatus,
)
from cloudguard.governance.remediation.executor import (
    ConcurrentExecutionError,
    ExecutionNotAllowedError,
    FixtureRemediationExecutor,
    LiveExecutionNotSupportedError,
    ProposalStaleError,
)
from cloudguard.governance.remediation.identity import (
    DevelopmentIdentityProvider,
    IdentityError,
    StsCallerIdentityProvider,
)
from cloudguard.governance.remediation.models import EventType, OperatingMode, ProposalIntegrityError, ProposalState
from cloudguard.governance.remediation.repository import (
    DynamoProposalRepository,
    IdempotencyConflictError,
    LeaseConflictError,
    LocalProposalRepository,
    ProposalManager,
    ProposalNotFoundError,
)
from cloudguard.governance.remediation.revalidation import revalidate
from cloudguard.governance.remediation.writers import (
    ApprovedWrite,
    FaultKind,
    FixtureAccount,
    FixtureWriter,
    ResourceSnapshot,
    SimulatedCrash,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "backend" / "fixtures" / "custom-tagging-demo.json"
ACCT = "123456789012"
T0 = datetime(2026, 10, 14, 6, 30, tzinfo=UTC)
TABLE = "cloudguard-governance-test"
CONFIG = {"accounts": {ACCT: {"admin": ["dev:admin"], "create": ["dev:alice"], "approve": ["dev:bob"],
                              "reject": ["dev:bob"], "execute": ["dev:alice", "dev:erin"]}}}
LAMBDA = f"arn:aws:lambda:ap-south-1:{ACCT}:function:"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


class Clock:
    def __init__(self, now=T0 + timedelta(minutes=1)):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(params=["memory", "disk", "dynamodb"])
def repo(request, tmp_path):
    if request.param == "memory":
        yield LocalProposalRepository()
    elif request.param == "disk":
        yield LocalProposalRepository(tmp_path / "proposals")
    else:
        with mock_aws():
            client = boto3.client("dynamodb", region_name="ap-south-1", aws_access_key_id="t", aws_secret_access_key="t")
            client.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                                      {"AttributeName": "SK", "AttributeType": "S"}],
                                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                           {"AttributeName": "SK", "KeyType": "RANGE"}])
            yield DynamoProposalRepository(client, TABLE)


class World:
    """Simulated account + stores + identities, with an approved proposal ready to execute."""

    def __init__(self, repo, request="Tag all Lambda functions with Project=CloudGuard", config=CONFIG,
                 ttl=timedelta(minutes=15), identities=True, policies=None):
        self.clock = Clock()
        self.account = FixtureAccount.from_discovery(FixtureSource(FIXTURE).discover())
        self.policies = policies or PolicyManager(LocalPolicyStore())
        self.policies.ensure_default()
        self.proposals = ProposalManager(repo, clock=self.clock, policy_manager=self.policies)
        self.authz = seeded_authz(config)
        self.repo = repo
        self.proposal = CustomTaggingEngine(self.account, self.policies.get_active(), clock=lambda: T0,
                                            proposal_ttl=ttl).prepare(request).proposal
        self.changing = [r.resource_arn for r in self.proposal.records if r.changes]
        self.svc("alice").create_proposal(
            self.proposal, idempotency_key="key-create-0001",
            resource_identities=self.account.identities(self.changing) if identities else None)
        self.approval = self.svc("bob").approve(
            self.proposal.proposal_id, content_hash=self.proposal.content_hash, nonce=new_nonce(),
            idempotency_key="key-approve-001",
            acknowledge_overwrites=self.proposal.requires_overwrite_confirmation).decision

    def svc(self, name):
        return DecisionService(self.proposals, self.authz, DevelopmentIdentityProvider(name, clock=self.clock),
                               clock=self.clock)

    def executor(self, name="alice", **kw):
        return FixtureRemediationExecutor(self.svc(name), self.account, clock=self.clock, **kw)

    def execute(self, name="alice", key="key-exec-00001", **kw):
        return self.executor(name, **kw).execute(self.proposal.proposal_id, approval_id=self.approval.decision_id,
                                                 idempotency_key=key)

    def tags(self, arn):
        return dict(self.account.resources[arn].tags)

    def state(self):
        return self.proposals.head(self.proposal.proposal_id).state


def by_name(view, name, region="ap-south-1"):
    arn = next(a for a in view.record.intended_resources if a.endswith(f":function:{name}") and f":{region}:" in a) \
        if ":" not in name else name
    return view.results[arn]


# ================================================================ authorisation and approval

def test_valid_fixture_execution_succeeds(repo):
    w = World(repo)
    before = {a: w.tags(a) for a in w.account.resources}
    view = w.execute()
    assert view.summary.outcome == ProposalState.SUCCEEDED and view.summary.succeeded == 4
    assert w.state() == ProposalState.SUCCEEDED
    for arn in w.changing:
        assert w.tags(arn) == {**before[arn], "Project": "CloudGuard"}  # unrelated tags preserved
    for arn in set(w.account.resources) - set(w.changing):
        assert w.tags(arn) == before[arn]  # nothing outside the approved scope touched
    assert view.record.requested_by.id == "dev:alice" and view.record.approver.id == "dev:bob"
    assert view.record.mode == OperatingMode.FIXTURE and view.lease.released
    events = w.proposals.history(w.proposal.proposal_id)
    assert [e.event_type for e in events][-2:] == [EventType.EXECUTION_STARTED, EventType.EXECUTION_SUCCEEDED]


def test_invalid_or_wrong_approval_ids_are_refused(repo):
    w = World(repo)
    ex = w.executor()
    with pytest.raises(ExecutionNotAllowedError, match="approval id"):
        ex.execute(w.proposal.proposal_id, approval_id="decision-00000000-0000-4000-8000-000000000000",
                   idempotency_key="key-exec-badid")
    with pytest.raises(ProposalNotFoundError):
        ex.execute("proposal-00000000-0000-4000-8000-000000000000", approval_id=w.approval.decision_id,
                   idempotency_key="key-exec-badpr")
    assert w.state() == ProposalState.APPROVED and not w.account.write_log


def test_content_hash_tampering_blocks_execution(repo):
    w = World(repo)
    meta = w.repo.get_stored(w.proposal.proposal_id)
    if isinstance(repo, DynamoProposalRepository):
        repo.client.update_item(TableName=TABLE, Key={"PK": {"S": f"PROPOSAL#{w.proposal.proposal_id}"}, "SK": {"S": "META"}},
                                UpdateExpression="SET content_hash = :h", ExpressionAttributeValues={":h": {"S": "sha256:" + "0" * 64}})
    else:
        repo.files.replace(f"proposals/{w.proposal.proposal_id}/META.json",
                           meta.model_copy(update={"content_hash": "sha256:" + "0" * 64}).model_dump_json())
    with pytest.raises(ProposalIntegrityError, match="content_hash"):
        w.execute()
    assert not w.account.write_log


def test_expired_approval_cannot_execute(repo):
    w = World(repo, ttl=timedelta(hours=1))
    w.clock.now = w.approval.approval_expires_at
    with pytest.raises(ExecutionNotAllowedError, match="EXPIRED"):
        w.execute()
    assert w.state() == ProposalState.EXPIRED and not w.account.write_log


def test_revoked_approval_cannot_execute(repo):
    w = World(repo)
    w.svc("bob").revoke(w.proposal.proposal_id, content_hash=w.proposal.content_hash,
                        approval_id=w.approval.decision_id, nonce=new_nonce(), idempotency_key="key-revoke-01", reason="no")
    with pytest.raises(ExecutionNotAllowedError, match="REVOKED"):
        w.execute()
    assert not w.account.write_log


def test_live_mode_is_refused_everywhere(repo):
    w = World(repo)

    class FakeSts:
        def get_caller_identity(self):
            return {"Arn": f"arn:aws:iam::{ACCT}:user/alice", "Account": ACCT, "UserId": "x"}

    live = DecisionService(w.proposals, w.authz, StsCallerIdentityProvider(FakeSts(), clock=w.clock), clock=w.clock)
    with pytest.raises(LiveExecutionNotSupportedError):
        FixtureRemediationExecutor(live, w.account)

    class DevPretendingLive:
        mode = OperatingMode.LIVE

        def current(self):
            return DevelopmentIdentityProvider("alice").current()

    with pytest.raises(LiveExecutionNotSupportedError):
        FixtureRemediationExecutor(DecisionService(w.proposals, w.authz, DevPretendingLive()), w.account)
    ex = w.executor()
    ex.decisions.identity = DevPretendingLive()  # even if swapped after construction
    with pytest.raises(LiveExecutionNotSupportedError):
        ex.execute(w.proposal.proposal_id, approval_id=w.approval.decision_id, idempotency_key="key-exec-swap1")
    assert not w.account.write_log


def test_development_identity_in_live_mode_is_refused():
    with pytest.raises(IdentityError):
        from cloudguard.governance.remediation.identity import require_identity_for_mode
        require_identity_for_mode(DevelopmentIdentityProvider("alice").current(), OperatingMode.LIVE)


def test_unauthorised_execution_requester(repo):
    w = World(repo)
    with pytest.raises(AuthorizationError, match="lacks 'execute'"):
        w.execute(name="bob")  # approver, but not an executor
    assert w.state() == ProposalState.APPROVED and not w.account.write_log


def test_removed_approver_or_changed_allow_list_blocks_execution(repo):
    w = World(repo)
    cfg = json.loads(json.dumps(CONFIG))
    cfg["accounts"][ACCT]["approve"] = ["dev:carol"]
    edit_authz(w.authz, cfg, OperatingMode.FIXTURE, note="remove bob")
    with pytest.raises(ExecutionNotAllowedError, match="no longer authorised"):
        w.execute()
    assert w.state() == ProposalState.APPROVED and not w.account.write_log


def test_changed_tagging_policy_marks_proposal_stale(repo, tmp_path):
    pm = PolicyManager(LocalPolicyStore(tmp_path / "pol"))
    w = World(repo, policies=pm)
    data = DEFAULT_POLICY_DOCUMENT.model_dump()
    data["defaults"].append({"key": "Team"})
    pm.save(parse_policy(data), expected_version=1, created_by="admin")
    with pytest.raises(ProposalStaleError, match="STALE"):
        w.execute()
    assert w.state() == ProposalState.STALE and not w.account.write_log


def test_wrong_account_blocks_entire_execution(repo):
    w = World(repo)
    w.account.account_id = "210987654321"  # the simulated account is not the proposal's account
    with pytest.raises(ExecutionNotAllowedError, match="ACCOUNT_MISMATCH"):
        w.execute()
    assert w.state() == ProposalState.APPROVED and not w.account.write_log


# ================================================================ revalidation rules (pure)

def _record(w, name):
    return next(r for r in w.proposal.records if r.resource_arn == LAMBDA + name)


def _snap(w, arn, **over):
    r = w.account.resources[arn]
    base = dict(exists=True, arn=arn, resource_type=r.resource_type, account_id=r.account_id, region=r.region,
                uid=r.uid, uid_kind=r.uid_kind, tags=dict(r.tags), request_id="req")
    return ResourceSnapshot(**{**base, **over})


def _rv(w, record, snapshot, baseline="same"):
    baseline_uid = snapshot.uid if baseline == "same" else baseline
    return revalidate(record, snapshot, account_id=ACCT, baseline_uid=baseline_uid,
                      original_rules=DEFAULT_POLICY_DOCUMENT.rules_for(record.resource_type))


def test_revalidation_statuses(repo):
    w = World(repo)
    rec = _record(w, "image-resizer")
    arn = rec.resource_arn
    S = RevalidationStatus
    assert _rv(w, rec, _snap(w, arn)).status == S.ELIGIBLE
    assert _rv(w, rec, ResourceSnapshot(exists=False, arn=arn)).status == S.RESOURCE_MISSING
    assert _rv(w, rec, _snap(w, arn, tags={"Project": "Other"})).status == S.TAG_DRIFT  # add-key conflict
    assert _rv(w, rec, _snap(w, arn, tags={"project": "x"})).status == S.TAG_DRIFT  # case variant appeared
    assert _rv(w, rec, _snap(w, arn, tags={"Project": "CloudGuard"})).status == S.ALREADY_APPLIED
    assert _rv(w, rec, _snap(w, arn, tags={"cloudguard:protected": "TRUE"})).status == S.PROTECTED_TAG
    assert _rv(w, rec, _snap(w, arn, tags={f"k{i}": "v" for i in range(50)})).status == S.TAG_LIMIT_EXCEEDED
    assert _rv(w, rec, _snap(w, arn, account_id="210987654321")).status == S.ACCOUNT_MISMATCH
    assert _rv(w, rec, _snap(w, arn, region="us-east-1")).status == S.REGION_MISMATCH
    unrelated = _rv(w, rec, _snap(w, arn, tags={"Team": "imaging"}))  # unrelated drift never blocks Lambda
    assert unrelated.status == S.ELIGIBLE and unrelated.pending[0].key == "Project"


def test_update_revalidation_requires_the_approved_previous_value(repo):
    w = World(repo, request="Update Owner of all Lambda functions to platform-team")
    rec = next(r for r in w.proposal.records if r.resource_arn == LAMBDA + "billing-api")
    arn = rec.resource_arn
    S = RevalidationStatus
    assert _rv(w, rec, _snap(w, arn)).status == S.ELIGIBLE
    gone = {k: v for k, v in w.tags(arn).items() if k != "Owner"}
    assert _rv(w, rec, _snap(w, arn, tags=gone)).status == S.TAG_DRIFT  # update key missing
    assert _rv(w, rec, _snap(w, arn, tags={**w.tags(arn), "Owner": "someone"})).status == S.TAG_DRIFT
    assert _rv(w, rec, _snap(w, arn, tags={**w.tags(arn), "Owner": "platform-team"})).status == S.ALREADY_APPLIED


def test_immutable_identity_checks(repo):
    w = World(repo, request="Add Owner=data-team to all DynamoDB tables")
    rec = next(r for r in w.proposal.records if r.changes)
    arn = rec.resource_arn
    baseline = w.repo.get_stored(w.proposal.proposal_id).resource_identities[arn]
    assert _rv(w, rec, _snap(w, arn), baseline=baseline).status == RevalidationStatus.ELIGIBLE
    w.account.recreate_resource(arn, tags={})
    assert _rv(w, rec, _snap(w, arn), baseline=baseline).status == RevalidationStatus.RESOURCE_RECREATED
    lam = World(LocalProposalRepository())
    lrec = _record(lam, "image-resizer")
    note = _rv(lam, lrec, _snap(lam, lrec.resource_arn), baseline=None).identity_note
    assert "cannot be detected" in note  # Lambda: no universal immutable identifier


def test_s3_requires_the_whole_tag_set_to_be_unchanged(repo):
    w = World(repo, request="Tag bucket acme-logs with Owner=storage-team")
    rec = next(r for r in w.proposal.records if r.changes)
    arn = rec.resource_arn
    assert _rv(w, rec, _snap(w, arn)).status == RevalidationStatus.ELIGIBLE
    drifted = _snap(w, arn, tags={**w.tags(arn), "Retention": "30d"})
    assert _rv(w, rec, drifted).status == RevalidationStatus.TAG_DRIFT


def test_revalidation_never_widens_or_invents_changes(repo):
    w = World(repo)
    rec = _record(w, "image-resizer")
    result = _rv(w, rec, _snap(w, rec.resource_arn))
    assert [(c.key, c.action, c.expected_current, c.new_value) for c in result.pending] == \
        [(c.key, c.action, c.old_value, c.new_value) for c in rec.changes]


# ================================================================ execution behaviour

def test_single_resource_success(repo):
    w = World(repo, request="Tag Lambda function image-resizer with Team=imaging")
    view = w.execute()
    assert view.summary.outcome == ProposalState.SUCCEEDED and view.summary.intended == 1
    assert w.tags(LAMBDA + "image-resizer") == {"Team": "imaging"}


def test_partial_success_with_drift_and_no_rollback(repo):
    w = World(repo)
    w.account.external_tag_change(LAMBDA + "image-resizer", "Project", "Someone-Else")
    view = w.execute()
    assert view.summary.outcome == ProposalState.PARTIAL
    skipped = by_name(view, "image-resizer")
    assert (skipped.status, skipped.revalidation, skipped.error_category) == \
        (ResultStatus.SKIPPED, RevalidationStatus.TAG_DRIFT, ErrorCategory.INELIGIBLE)
    assert w.tags(LAMBDA + "image-resizer")["Project"] == "Someone-Else"  # never overwritten
    assert w.tags(LAMBDA + "shared-name")["Project"] == "CloudGuard"  # successes kept, no rollback
    with pytest.raises(ExecutionNotAllowedError, match="terminal"):
        w.execute(key="key-exec-again1")


def test_all_resources_fail_without_writes(repo):
    w = World(repo)
    for arn in w.changing:
        w.account.external_tag_change(arn, "Project", "Other")
    view = w.execute()
    assert view.summary.outcome == ProposalState.FAILED and view.summary.skipped == 4
    assert not w.account.write_log and w.state() == ProposalState.FAILED


def test_already_applied_resources_are_not_rewritten(repo):
    w = World(repo)
    w.account.external_tag_change(LAMBDA + "image-resizer", "Project", "CloudGuard")
    view = w.execute()
    assert by_name(view, "image-resizer").status == ResultStatus.ALREADY_APPLIED
    assert LAMBDA + "image-resizer" not in [a for a, _ in w.account.write_log]
    assert view.summary.outcome == ProposalState.SUCCEEDED


@pytest.mark.parametrize("kind", [FaultKind.SILENT_NO_OP, FaultKind.STALE_READ])
def test_verification_failures(repo, kind):
    w = World(repo, request="Tag Lambda function image-resizer with Team=imaging")
    arn = LAMBDA + "image-resizer"
    if kind == FaultKind.SILENT_NO_OP:
        w.account.inject(arn, "write", kind)
    else:
        w.account.inject(arn, "read:verify", kind, times=5)
    result = w.execute().results[arn]
    assert result.status == ResultStatus.FAILED_VERIFICATION and result.error_category == ErrorCategory.VERIFICATION
    assert w.state() == ProposalState.FAILED


def test_eventually_consistent_read_back_is_retried(repo):
    w = World(repo, request="Tag Lambda function image-resizer with Team=imaging")
    w.account.inject(LAMBDA + "image-resizer", "read:verify", FaultKind.STALE_READ, times=2)
    assert w.execute().results[LAMBDA + "image-resizer"].status == ResultStatus.APPLIED_VERIFIED


@pytest.mark.parametrize("kind", [FaultKind.THROTTLE, FaultKind.TRANSIENT])
def test_bounded_retry_of_transient_failures(repo, kind):
    w = World(repo, request="Tag Lambda function image-resizer with Team=imaging")
    arn = LAMBDA + "image-resizer"
    w.account.inject(arn, "write", kind, times=2)  # succeeds on the 3rd attempt
    ok = w.execute()
    assert ok.results[arn].status == ResultStatus.APPLIED_VERIFIED and ok.results[arn].attempts == 3
    w2 = World(LocalProposalRepository(), request="Tag Lambda function image-resizer with Team=imaging")
    w2.account.inject(arn, "write", kind, times=10)  # exhausts the bound
    failed = w2.execute().results[arn]
    assert failed.status == ResultStatus.FAILED_WRITE
    assert failed.error_category == (ErrorCategory.THROTTLED if kind == FaultKind.THROTTLE else ErrorCategory.TRANSIENT)
    assert failed.attempts == 3


def test_unretryable_failures_are_not_retried(repo):
    w = World(repo, request="Tag Lambda function image-resizer with Team=imaging")
    arn = LAMBDA + "image-resizer"
    w.account.inject(arn, "read:revalidate", FaultKind.ACCESS_DENIED)
    result = w.execute().results[arn]
    assert result.status == ResultStatus.SKIPPED and result.revalidation == RevalidationStatus.INSPECTION_FAILED
    assert [p for a, p in w.account.read_log if a == arn].count("revalidate") == 1  # no retry


def test_unverifiable_after_persistent_throttling(repo):
    w = World(repo, request="Tag Lambda function image-resizer with Team=imaging")
    arn = LAMBDA + "image-resizer"
    w.account.inject(arn, "read:revalidate", FaultKind.THROTTLE, times=10)
    result = w.execute().results[arn]
    assert result.revalidation == RevalidationStatus.UNVERIFIABLE and not w.account.write_log


def test_missing_and_recreated_resources_are_skipped(repo):
    w = World(repo, request="Add Owner=data-team to all DynamoDB tables")
    changing = list(w.changing)
    w.account.delete_resource(changing[0])
    w.account.recreate_resource(changing[1], tags={})
    view = w.execute()
    assert view.results[changing[0]].revalidation == RevalidationStatus.RESOURCE_MISSING
    assert view.results[changing[1]].revalidation == RevalidationStatus.RESOURCE_RECREATED
    assert all(a not in (changing[0], changing[1]) for a, _ in w.account.write_log)


def test_concurrent_modification_between_read_and_write_is_reported(repo):
    w = World(repo, request="Tag Lambda function image-resizer with Team=imaging")
    arn = LAMBDA + "image-resizer"

    def interfere(point, target):
        if point == "before_write" and target == arn:
            w.account.external_tag_change(arn, "Env", "Staging")  # another actor, after our pre-write read

    result = w.execute(hooks=interfere).results[arn]
    assert result.status == ResultStatus.APPLIED_VERIFIED
    assert any("appeared during execution" in m for m in result.warnings)
    assert w.tags(arn) == {"Env": "Staging", "Team": "imaging"}


def test_drift_detected_at_pre_write_re_read(repo):
    w = World(repo, request="Tag Lambda function image-resizer with Team=imaging")
    arn = LAMBDA + "image-resizer"

    def drift(point, target):
        if point == "before_prewrite" and target == arn:
            w.account.external_tag_change(arn, "Team", "someone-else")

    result = w.execute(hooks=drift).results[arn]
    assert result.status == ResultStatus.SKIPPED and "pre-write" in result.detail
    assert w.tags(arn)["Team"] == "someone-else" and not w.account.write_log


# ================================================================ leases, crashes, retries, idempotency

def _crash_at(point_wanted, arn_wanted):
    def hook(point, arn):
        if point == point_wanted and arn == arn_wanted:
            raise SimulatedCrash(point)
    return hook


def test_crash_before_write_then_resume_after_lease_expiry(repo):
    w = World(repo)
    target = LAMBDA + "shared-name"
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", target))
    assert w.state() == ProposalState.EXECUTING and target not in [a for a, _ in w.account.write_log]
    with pytest.raises(ConcurrentExecutionError, match="in progress"):
        w.execute(name="erin", key="key-exec-other1")  # lease still held
    w.clock.now += timedelta(minutes=3)
    view = w.execute(name="erin", key="key-exec-other1")
    assert view.summary.outcome == ProposalState.SUCCEEDED and view.lease.token == 2
    assert [a for a, _ in w.account.write_log].count(target) == 1


def test_crash_after_write_records_already_applied_without_rewriting(repo):
    w = World(repo)
    target = LAMBDA + "shared-name"
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("after_write", target))
    assert w.tags(target)["Project"] == "CloudGuard"  # the write happened, its result didn't get recorded
    w.clock.now += timedelta(minutes=3)
    view = w.execute()  # same idempotency key: a retry
    assert view.results[target].status == ResultStatus.ALREADY_APPLIED
    assert [a for a, _ in w.account.write_log].count(target) == 1


def test_expired_lease_holder_is_fenced_out(repo):
    w = World(repo)
    target = LAMBDA + "shared-name"
    crashed = w.executor(hooks=_crash_at("before_write", target))
    with pytest.raises(SimulatedCrash):
        crashed.execute(w.proposal.proposal_id, approval_id=w.approval.decision_id, idempotency_key="key-exec-fence1")
    w.clock.now += timedelta(minutes=3)
    w.execute(name="erin", key="key-exec-fence2")  # recovers with token 2 and finishes
    record = w.repo.get_execution(w.proposal.proposal_id, w.proposals.head(w.proposal.proposal_id).execution_id)
    stale_event = w.repo.list_execution_events(record.proposal_id, record.execution_id)[0]
    with pytest.raises(LeaseConflictError):  # the old executor's token 1 can't write anything
        w.repo.append_execution_event(stale_event.model_copy(update={"seq": 99}), 1, w.clock.now)


def test_execution_window_elapsed_closes_without_writes(repo):
    w = World(repo)
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", w.changing[0]))
    writes = len(w.account.write_log)
    w.clock.now += timedelta(hours=1)
    view = w.execute(key="key-exec-late01")
    assert view.summary.outcome == ProposalState.FAILED and view.summary.not_attempted == 4
    assert len(w.account.write_log) == writes


def test_concurrent_start_requests_only_one_executes(repo):
    """While one execution holds the lease, a second request (other identity, other key) is refused."""
    w = World(repo)
    refused = []

    def second_request(point, arn):
        if point == "before_write" and not refused:
            with pytest.raises(ConcurrentExecutionError, match="in progress"):
                w.execute(name="erin", key="key-exec-concur")
            refused.append(True)

    view = w.execute(hooks=second_request, key="key-exec-main1")
    assert refused == [True] and view.summary.outcome == ProposalState.SUCCEEDED
    assert len(w.repo.list_executions(w.proposal.proposal_id)) == 1
    assert all(n == 1 for n in [[a for a, _ in w.account.write_log].count(arn) for arn in w.changing])


def test_execution_idempotency(repo):
    w = World(repo)
    first = w.execute(key="key-exec-idem01")
    again = w.execute(key="key-exec-idem01")
    assert again.record.execution_id == first.record.execution_id and again.summary == first.summary
    with pytest.raises(IdempotencyConflictError):
        w.execute(name="erin", key="key-exec-idem01")


# ================================================================ audit integrity

def test_execution_audit_is_append_only_and_hash_chained(repo):
    w = World(repo)
    view = w.execute()
    events = view.events
    assert events[0].prev_hash == view.record.record_hash()
    assert all(b.prev_hash == a.event_hash for a, b in zip(events, events[1:]))
    assert [e.event_type.value for e in events][-1] == "FINALIZED"
    for cls in (LocalProposalRepository, DynamoProposalRepository):
        assert not [n for n in dir(cls) if "exec" in n and any(w_ in n for w_ in ("delete", "update", "remove"))]


def _rewrite_exec_event(repo, event):
    pid, eid = event.proposal_id, event.execution_id
    if isinstance(repo, DynamoProposalRepository):
        repo.client.put_item(TableName=TABLE, Item=repo._exec_event_item(event))
    else:
        repo.files.replace(f"proposals/{pid}/EXEC-{eid}-EVT-{event.seq:010d}.json", event.model_dump_json())


@pytest.mark.parametrize("tamper", ["payload_rehashed", "result_without_rehash", "drop_middle"])
def test_tampered_execution_records_are_detected(repo, tamper):
    w = World(repo)
    view = w.execute()
    target = next(e for e in view.events if e.result is not None)
    forged_result = target.result.model_copy(update={"detail": "forged"})
    if tamper == "payload_rehashed":
        forged = target.model_copy(update={"result": forged_result})
        _rewrite_exec_event(repo, forged.model_copy(update={"event_hash": forged.compute_hash()}))
    elif tamper == "result_without_rehash":
        _rewrite_exec_event(repo, target.model_copy(update={"result": forged_result}))
    else:
        nxt = view.events[target.seq]  # the event after the target, re-chained onto the previous one
        relinked = nxt.model_copy(update={"seq": target.seq, "prev_hash": target.prev_hash})
        _rewrite_exec_event(repo, relinked.model_copy(update={"event_hash": relinked.compute_hash()}))
    with pytest.raises(ProposalIntegrityError):  # ExecutionIntegrityError is a subclass
        w.executor().load(w.proposal.proposal_id, view.record.execution_id)


def test_proposal_and_approval_records_are_unchanged_by_execution(repo):
    w = World(repo)
    stored_before = w.repo.get_stored(w.proposal.proposal_id)
    approval_before = w.repo.get_decision(w.proposal.proposal_id, w.approval.decision_id)
    w.execute()
    assert w.repo.get_stored(w.proposal.proposal_id) == stored_before
    assert w.repo.get_decision(w.proposal.proposal_id, w.approval.decision_id) == approval_before


# ================================================================ safety

def test_no_aws_tagging_api_is_ever_called(repo, monkeypatch):
    calls = []
    real_client = boto3.client

    def guarded(service, *a, **kw):
        calls.append(service)
        if service in ("lambda", "ec2", "s3", "resourcegroupstaggingapi"):
            raise AssertionError(f"execution must never create a {service} client")
        return real_client(service, *a, **kw)

    monkeypatch.setattr(boto3, "client", guarded)
    monkeypatch.setattr(boto3.Session, "client", lambda self, service, *a, **kw: guarded(service, *a, **kw))
    w = World(repo)
    w.execute()
    for module in (executor_module, writers_module):
        source = inspect.getsource(module)
        assert "boto3" not in source.replace("no boto3 import", "")
        for api in ("tag_resource", "put_bucket_tagging", "create_tags", "untag_resource", "delete_tags"):
            assert api not in source


def test_fixture_writer_only_targets_fixture_accounts():
    class Impostor(FixtureAccount):
        pass

    with pytest.raises(TypeError):
        FixtureWriter(Impostor(ACCT, ["ap-south-1"]))
    with pytest.raises(TypeError):
        FixtureWriter(object())
    account = FixtureAccount(ACCT, ["ap-south-1"])
    with pytest.raises(TypeError):
        FixtureWriter(account).apply({"resource_arn": "x", "changes": [{"key": "A", "new_value": "B"}]})
    with pytest.raises(ValueError):
        ApprovedWrite(resource_arn="x", changes=())


def test_writer_never_removes_or_touches_unrelated_tags():
    account = FixtureAccount.from_discovery(FixtureSource(FIXTURE).discover())
    arn = LAMBDA + "ServiceWRK"
    before = dict(account.resources[arn].tags)
    FixtureWriter(account).apply(ApprovedWrite(arn, (ApprovedChange(key="Team", action=ChangeAction.ADD,
                                                                    expected_current=None, new_value="x"),)))
    assert account.resources[arn].tags == {**before, "Team": "x"}


def test_execute_accepts_no_resources_changes_or_bodies():
    params = set(inspect.signature(FixtureRemediationExecutor.execute).parameters)
    assert params == {"self", "proposal_id", "approval_id", "idempotency_key"}
    for text in ("execute proposal now", "run the approved changes", "approve and execute"):
        assert not isinstance(RuleBasedParser().parse(text), TaggingRequest)


def test_no_environment_switch_to_live_execution(monkeypatch):
    for var in ("CLOUDGUARD_MODE", "CLOUDGUARD_LIVE", "CLOUDGUARD_EXECUTE_LIVE"):
        monkeypatch.setenv(var, "live")
    source = inspect.getsource(executor_module) + inspect.getsource(writers_module)
    assert "os.environ" not in source and "getenv" not in source


# ================================================================ CLI

def test_cli_fixture_execution_workflow(tmp_path, capsys, monkeypatch):
    authz = tmp_path / "authz.json"
    authz.write_text(json.dumps(CONFIG), encoding="utf-8")
    s = ["--proposal-store", str(tmp_path / "p"), "--policy-store", str(tmp_path / "pol")]
    az = ["--authz-store", str(tmp_path / "az")]
    acct = ["--fixture-account", str(tmp_path / "acct.json")]
    monkeypatch.setenv(BOOTSTRAP_ENV, "dev:admin")
    assert main(["authz", "save", str(authz), "--expected-version", "0", "--dev-identity", "admin", *az]) == 0
    assert main(["proposal", "save", "Tag all Lambda functions with Project=CloudGuard", "--dev-identity", "alice",
                 *s, *az, *acct]) == 0
    capsys.readouterr()
    assert main(["proposal", "save", "Tag all Lambda functions with Project=CloudGuard", "--dev-identity", "alice",
                 "--idempotency-key", "cli-save-0001", *s, *az, *acct]) == 0
    pid = json.loads(capsys.readouterr().out)["proposal_id"]
    assert main(["proposal", "show", pid, *s]) == 0
    h = json.loads(capsys.readouterr().out)["proposal"]["content_hash"]
    assert main(["proposal", "approve", pid, "--content-hash", h, "--dev-identity", "bob", *s, *az]) == 0
    aid = json.loads(capsys.readouterr().out)["decision"]["decision_id"]
    assert main(["proposal", "execute", pid, "--approval-id", aid, "--mode", "fixture", "--dev-identity", "alice",
                 *s, *az, *acct]) == 0
    out = capsys.readouterr()
    assert "simulated account only" in out.err and "APPLIED_VERIFIED" in out.err
    result = json.loads(out.out)
    assert result["simulated_account"] is True and result["execution"]["summary"]["outcome"] == "SUCCEEDED"
    eid = result["execution"]["record"]["execution_id"]
    assert main(["proposal", "execution-show", pid, eid, *s]) == 0
    assert json.loads(capsys.readouterr().out)["execution"]["record"]["execution_id"] == eid
    assert main(["proposal", "execution-history", pid, *s]) == 0
    assert json.loads(capsys.readouterr().out)[0]["outcome"] == "SUCCEEDED"
    state = json.loads((tmp_path / "acct.json").read_text(encoding="utf-8"))
    assert {r["name"]: r["tags"].get("Project") for r in state["resources"]
            if r["resource_type"] == "AWS::Lambda::Function"}["billing-api"] == "OtherProject"


@pytest.mark.parametrize("argv", [
    ["--mode", "live", "--dev-identity", "alice"],
    ["--dev-identity", "alice"],               # mode must be explicit
    ["--mode", "fixture"],                     # identity required
    ["--mode", "fixture", "--dev-identity", "alice", "--live"],
])
def test_cli_has_no_live_execution_path(argv):
    with pytest.raises(SystemExit) as exc:
        main(["proposal", "execute", "proposal-00000000-0000-4000-8000-000000000000", "--approval-id", "x", *argv])
    assert exc.value.code == 2
