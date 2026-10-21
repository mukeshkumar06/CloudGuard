"""Batch 2.1: fail-closed policy validation (L1), accurate abort summaries (A1) and boundary/race
regressions for the execution-safety code. In-memory, on-disk and DynamoDB (moto) via `repo`."""

from datetime import timedelta

import pytest

from cloudguard.governance.custom_tagging import CustomTaggingEngine
from cloudguard.governance.policy import DEFAULT_POLICY_DOCUMENT, parse_policy
from cloudguard.governance.policy_store import LocalPolicyStore, PolicyManager
from cloudguard.governance.remediation.approval import (
    DecisionNotAllowedError,
    DecisionService,
    PolicyValidationUnavailableError,
    new_nonce,
)
from cloudguard.governance.remediation.execution_models import ResourceResult, ResultStatus, summarize
from cloudguard.governance.remediation.executor import ExecutionNotAllowedError, LeaseLostError
from cloudguard.governance.remediation.identity import DevelopmentIdentityProvider
from cloudguard.governance.remediation.models import ProposalState
from cloudguard.governance.remediation.repository import LeaseConflictError, ProposalManager, TransitionConflictError
from cloudguard.governance.remediation.writers import SimulatedCrash
from test_execution import LAMBDA, T0, World, _crash_at, _isolated_cwd, repo  # noqa: F401 - fixtures
from test_execution_safety import abort, assert_every_write_was_announced, head, pid

TARGET = LAMBDA + "shared-name"


def change_policy(pm: PolicyManager):
    data = DEFAULT_POLICY_DOCUMENT.model_dump()
    data["defaults"].append({"key": "Team"})
    pm.save(parse_policy(data), expected_version=pm.get_active().version, created_by="admin")


def crashed_world(repo, arn=TARGET):
    w = World(repo)
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", arn))
    return w


# ================================================================ L1: fail-closed policy validation

def test_decision_service_requires_a_policy_manager(repo):
    w = World(repo)
    storage_only = ProposalManager(repo, clock=w.clock)  # storage-only use stays allowed
    assert storage_only.get(pid(w)).head.state == ProposalState.APPROVED
    with pytest.raises(ValueError, match="requires a ProposalManager with a policy_manager"):
        DecisionService(storage_only, w.authz, DevelopmentIdentityProvider("bob", clock=w.clock))


def test_approval_cannot_bypass_policy_validation_by_removing_the_policy_manager(repo):
    w = World(repo)
    other = CustomTaggingEngine(w.account, w.policies.get_active(), clock=lambda: T0).prepare(
        "Add Owner=data-team to all DynamoDB tables").proposal
    w.svc("alice").create_proposal(other, idempotency_key="key-create-0002")
    bob = w.svc("bob")
    w.proposals.policy_manager = None  # removed after the service was built
    with pytest.raises(PolicyValidationUnavailableError, match="policy validation is unavailable"):
        bob.approve(other.proposal_id, content_hash=other.content_hash, nonce=new_nonce(),
                    idempotency_key="key-approve-002", acknowledge_overwrites=other.requires_overwrite_confirmation)
    assert w.repo.get_head(other.proposal_id).state == ProposalState.PENDING  # not approved, not marked STALE


def test_execution_cannot_bypass_policy_validation_by_removing_the_policy_manager(repo):
    w = World(repo)
    executor = w.executor()
    w.proposals.policy_manager = None
    with pytest.raises(ExecutionNotAllowedError, match="policy validation is unavailable"):
        executor.execute(pid(w), approval_id=w.approval.decision_id, idempotency_key="key-exec-nopm1")
    assert head(w).state == ProposalState.APPROVED and not w.account.write_log


def test_resume_cannot_bypass_policy_validation_by_removing_the_policy_manager(repo):
    w = crashed_world(repo)
    writes = len(w.account.write_log)
    executor = w.executor("erin")
    w.proposals.policy_manager = None
    w.clock.now += timedelta(minutes=3)
    view = executor.execute(pid(w), approval_id=w.approval.decision_id, idempotency_key="key-exec-nopm2")
    assert len(w.account.write_log) == writes
    assert "policy validation is unavailable" in view.results[TARGET].detail


# ================================================================ A1: accurate summaries

def _result(arn, status):
    return ResourceResult(resource_arn=arn, status=status, revalidation=None)


ARNS = [f"{LAMBDA}fn-{i}" for i in range(4)]


def test_summary_with_one_in_flight_write_and_three_unattempted_resources():
    s = summarize(ARNS, {}, aborted=True, in_flight=[ARNS[0]])
    assert (s.outcome, s.in_flight, s.unconfirmed, s.not_attempted, s.succeeded, s.failed) == \
        (ProposalState.ABORTED, [ARNS[0]], 1, 3, 0, 0)


@pytest.mark.parametrize(("statuses", "kwargs", "expected"), [
    ([ResultStatus.APPLIED_VERIFIED] * 3 + [ResultStatus.ALREADY_APPLIED], {},
     dict(outcome=ProposalState.SUCCEEDED, succeeded=4, already_applied=1, failed=0, not_attempted=0, unconfirmed=0)),
    ([ResultStatus.SKIPPED, ResultStatus.FAILED_WRITE, ResultStatus.FAILED_VERIFICATION, ResultStatus.NOT_ATTEMPTED], {},
     dict(outcome=ProposalState.FAILED, succeeded=0, skipped=1, failed=2, not_attempted=1, unconfirmed=0)),
    ([ResultStatus.APPLIED_VERIFIED, ResultStatus.UNCONFIRMED_WRITE, ResultStatus.NOT_ATTEMPTED,
      ResultStatus.NOT_ATTEMPTED], {},
     dict(outcome=ProposalState.PARTIAL, succeeded=1, failed=0, not_attempted=2, unconfirmed=1)),
    ([ResultStatus.APPLIED_VERIFIED, None, None, None], {"aborted": True},
     dict(outcome=ProposalState.ABORTED, succeeded=1, not_attempted=3, unconfirmed=0, in_flight=[])),
    ([ResultStatus.APPLIED_VERIFIED, None, None, None], {"aborted": True, "in_flight": [ARNS[1]]},
     dict(outcome=ProposalState.ABORTED, succeeded=1, not_attempted=2, unconfirmed=1, in_flight=[ARNS[1]])),
])
def test_summary_counts(statuses, kwargs, expected):
    results = {a: _result(a, s) for a, s in zip(ARNS, statuses) if s is not None}
    s = summarize(ARNS, results, **kwargs)
    assert {k: getattr(s, k) for k in expected} == expected
    assert s.succeeded + s.skipped + s.failed + s.not_attempted + s.unconfirmed == s.intended == 4


def test_in_flight_must_be_an_intended_resource_without_a_result():
    with pytest.raises(ValueError, match="in-flight"):
        summarize(ARNS, {ARNS[0]: _result(ARNS[0], ResultStatus.APPLIED_VERIFIED)}, aborted=True, in_flight=[ARNS[0]])


def test_aborted_execution_with_the_first_write_in_flight_counts_correctly(repo):
    w = World(repo)
    first = w.executor().decisions.proposals.get(pid(w)).proposal.records  # order = intended order
    first = [r.resource_arn for r in first if r.changes][0]

    def abort_now(point, arn):
        if point == "after_intent" and arn == first and not abort_now.done:
            abort_now.done = True
            abort(w)
    abort_now.done = False
    with pytest.raises(LeaseLostError):
        w.execute(hooks=abort_now)
    s = w.executor().load(pid(w), head(w).execution_id).summary
    assert (s.outcome, s.in_flight, s.unconfirmed, s.not_attempted, s.succeeded, s.failed) == \
        (ProposalState.ABORTED, [first], 1, 3, 0, 0)
    assert_every_write_was_announced(w)


# ================================================================ boundary and race regressions

def test_policy_change_between_two_writes_stops_the_second(repo, tmp_path):
    pm = PolicyManager(LocalPolicyStore(tmp_path / "pol"))
    w = World(repo, policies=pm)
    seen = []

    def change_after_first_write(point, arn):
        if point == "before_write":
            seen.append(arn)
            if len(seen) == 2:
                change_policy(pm)
    view = w.execute(hooks=change_after_first_write)
    assert len(w.account.write_log) == 1 and view.summary.outcome == ProposalState.PARTIAL
    assert "tagging policy changed" in view.results[seen[1]].detail


def test_approval_expiry_during_execution_stops_further_writes(repo):
    w = World(repo)
    expiry = head(w).approval_expires_at
    w.clock.now = expiry - timedelta(seconds=40)
    seen = []

    def reach_expiry(point, arn):
        if point == "before_write":
            seen.append(arn)
            if len(seen) == 2:
                w.clock.now = expiry  # exactly at expiry; the lease is still valid
    view = w.execute(hooks=reach_expiry)
    assert len(w.account.write_log) == 1 and view.summary.outcome == ProposalState.PARTIAL
    assert view.results[seen[1]].status == ResultStatus.NOT_ATTEMPTED and "approval expired" in view.results[seen[1]].detail


def test_abort_loses_to_a_finalisation_that_commits_first(repo):
    w = crashed_world(repo)
    w.clock.now += timedelta(minutes=3)
    real = w.repo.record_abort

    def finalise_first(*args):
        w.execute(name="erin", key="key-exec-race-1")  # takes over and finalises
        return real(*args)
    w.repo.record_abort = finalise_first
    with pytest.raises(TransitionConflictError):
        abort(w)
    w.repo.record_abort = real
    assert head(w).state == ProposalState.SUCCEEDED  # one consistent terminal state; history verifies
    assert w.executor().load(pid(w), head(w).execution_id).summary.outcome == ProposalState.SUCCEEDED


def test_finalisation_loses_to_an_abort_that_commits_first(repo):
    w = World(repo)
    real = w.repo.finish_execution

    def abort_first(*args):
        abort(w)
        return real(*args)
    w.repo.finish_execution = abort_first
    with pytest.raises(LeaseLostError):
        w.execute()
    w.repo.finish_execution = real
    assert head(w).state == ProposalState.ABORTED
    assert w.executor().load(pid(w), head(w).execution_id).summary.outcome == ProposalState.ABORTED


def test_abort_and_lease_takeover_conflict_in_both_orders(repo):
    w = crashed_world(repo)
    w.clock.now += timedelta(minutes=3)
    execution_id = head(w).execution_id
    lease = w.repo.get_lease(pid(w), execution_id)
    taken = lease.model_copy(update={"owner": "executor-b", "token": lease.token + 1, "acquired_at": w.clock.now,
                                     "expires_at": w.clock.now + timedelta(minutes=2)})
    real = w.repo.record_abort

    def take_over_first(*args):
        w.repo.take_over_lease(lease, taken)
        return real(*args)
    w.repo.record_abort = take_over_first
    with pytest.raises(TransitionConflictError):  # the abort's lease expectation is stale
        abort(w)
    w.repo.record_abort = real
    assert head(w).state == ProposalState.EXECUTING and w.repo.get_lease(pid(w), execution_id).token == taken.token
    abort(w, key="key-abort-0002")  # a fresh abort against the new lease succeeds
    with pytest.raises(LeaseConflictError):  # and a takeover after the abort is refused
        w.repo.take_over_lease(taken, taken.model_copy(update={"token": taken.token + 1}))
    assert head(w).state == ProposalState.ABORTED


@pytest.mark.parametrize(("seconds_before_expiry", "allowed"), [(10, False), (11, True)])
def test_lease_safety_margin_boundary(repo, seconds_before_expiry, allowed):
    """A write intent needs the lease to stay valid for MORE than the 10 s safety margin."""
    w = World(repo)

    def near_expiry(point, arn):
        if point == "before_write" and arn == TARGET and not near_expiry.done:
            near_expiry.done = True
            lease = w.repo.get_lease(pid(w), head(w).execution_id)
            w.clock.now = lease.expires_at - timedelta(seconds=seconds_before_expiry)
    near_expiry.done = False
    if allowed:
        w.execute(hooks=near_expiry)
        assert [a for a, _ in w.account.write_log].count(TARGET) == 1
    else:
        with pytest.raises(LeaseLostError):
            w.execute(hooks=near_expiry)
        assert [a for a, _ in w.account.write_log].count(TARGET) == 0
