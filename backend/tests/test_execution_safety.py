"""Batch 2: execution safety - approval expiry and ABORT (M1, L2) and lease fencing of writes (M2).

Runs against the in-memory, on-disk and DynamoDB (moto) repositories via the shared `repo`
fixture. Everything is fixture-only: no AWS API is called.
"""

from collections import Counter
from datetime import timedelta

import pytest

from cloudguard.governance.policy import DEFAULT_POLICY_DOCUMENT, parse_policy
from cloudguard.governance.policy_store import LocalPolicyStore, PolicyManager
from cloudguard.governance.remediation.approval import AuthorizationError, DecisionNotAllowedError, new_nonce
from cloudguard.governance.remediation.execution_models import ExecutionEventType, ResultStatus
from cloudguard.governance.remediation.executor import (
    ExecutionNotAllowedError,
    FixtureRemediationExecutor,
    LeaseLostError,
)
from cloudguard.governance.remediation.models import DecisionType, OperatingMode, ProposalState
from cloudguard.governance.remediation.repository import ApprovalNoLongerValidError, LeaseConflictError
from cloudguard.governance.remediation.writers import FaultKind, SimulatedCrash
from test_execution import LAMBDA, World, _crash_at, _isolated_cwd, repo  # noqa: F401 - fixtures

TARGET = LAMBDA + "shared-name"


def pid(w):
    return w.proposal.proposal_id


def head(w):
    return w.proposals.head(pid(w))


def writes_to(w, arn):
    return [a for a, _ in w.account.write_log].count(arn)


def abort(w, name="bob", key="key-abort-0001", execution_id=None):
    return w.svc(name).abort(pid(w), content_hash=w.proposal.content_hash,
                             execution_id=execution_id or head(w).execution_id, nonce=new_nonce(),
                             idempotency_key=key, reason="stop now")


def assert_every_write_was_announced(w):
    """Every write that reached the (simulated) account is preceded by a committed WRITE_INTENT
    for exactly that resource and those changes."""
    intents = Counter()
    for view in w.executor().history(pid(w)):
        for e in view.events:
            if e.event_type == ExecutionEventType.WRITE_INTENT:
                intents[(e.resource_arn, frozenset((c.key, c.new_value) for c in e.intent_changes))] += 1
    writes = Counter((arn, frozenset(tags.items())) for arn, tags in w.account.write_log)
    for key, count in writes.items():
        assert intents[key] >= count, f"unannounced write: {key}"


# ================================================================ M2: the exact reproduced scenario

def test_review_scenario_stale_executor_never_overwrites_a_later_human_change(repo):
    """Security review reproduction: executor A stalls just before its write, its lease expires, B
    takes over and finalises, a human then changes the tag - A must not overwrite it."""
    w = World(repo)

    def stall(point, arn):
        if point == "before_write" and arn == TARGET and not stall.done:
            stall.done = True
            w.clock.now += timedelta(minutes=3)              # A stalls past its lease
            assert w.execute(name="erin", key="key-exec-take-1").summary.outcome == ProposalState.SUCCEEDED
            w.account.external_tag_change(TARGET, "Project", "SomeoneElse")  # human change after finalisation
    stall.done = False

    with pytest.raises(LeaseLostError):
        w.execute(hooks=stall)
    assert w.tags(TARGET)["Project"] == "SomeoneElse"   # the human's value survives
    assert writes_to(w, TARGET) == 1                    # only B's write
    assert head(w).state == ProposalState.SUCCEEDED
    assert_every_write_was_announced(w)


def test_stale_executor_cannot_write_after_takeover_even_before_finalisation(repo):
    w = World(repo)

    def stall(point, arn):
        if point == "before_write" and arn == TARGET and not stall.done:
            stall.done = True
            w.clock.now += timedelta(minutes=3)
            lease = w.repo.get_lease(pid(w), head(w).execution_id)
            w.repo.take_over_lease(lease, lease.model_copy(update={"owner": "executor-b", "token": lease.token + 1,
                                                                   "acquired_at": w.clock.now,
                                                                   "expires_at": w.clock.now + timedelta(minutes=2)}))
    stall.done = False
    with pytest.raises(LeaseLostError):
        w.execute(hooks=stall)
    assert writes_to(w, TARGET) == 0 and head(w).state == ProposalState.EXECUTING


def test_stall_after_the_intent_past_the_deadline_issues_no_write(repo):
    """A stalls between its WRITE_INTENT and the write; B recovers and finishes; A's deadline has
    passed, so A issues nothing and can't record anything either."""
    w = World(repo)

    def stall(point, arn):
        if point == "after_intent" and arn == TARGET and not stall.done:
            stall.done = True
            w.clock.now += timedelta(minutes=3)  # past A's write deadline and lease
            w.execute(name="erin", key="key-exec-take-2")
            w.account.external_tag_change(TARGET, "Project", "SomeoneElse")
    stall.done = False
    with pytest.raises(LeaseLostError):
        w.execute(hooks=stall)
    assert w.tags(TARGET)["Project"] == "SomeoneElse" and writes_to(w, TARGET) == 1
    assert_every_write_was_announced(w)


def test_expired_lease_holder_cannot_renew_even_without_a_takeover(repo):
    w = World(repo)
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", TARGET))
    execution_id = head(w).execution_id
    lease = w.repo.get_lease(pid(w), execution_id)
    events = w.repo.list_execution_events(pid(w), execution_id)
    w.clock.now = lease.expires_at + timedelta(seconds=1)  # expired; nobody has taken over
    late = events[-1].model_copy(update={"seq": len(events) + 1, "at": w.clock.now})
    with pytest.raises(LeaseConflictError):
        w.repo.append_execution_event(late, lease.token, w.clock.now + timedelta(minutes=2))
    assert w.repo.get_lease(pid(w), execution_id) == lease  # not renewed


def test_a_live_lease_cannot_be_taken_over(repo):
    w = World(repo)
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", TARGET))
    lease = w.repo.get_lease(pid(w), head(w).execution_id)
    with pytest.raises(LeaseConflictError):
        w.repo.take_over_lease(lease, lease.model_copy(update={"owner": "x", "token": lease.token + 1,
                                                               "acquired_at": w.clock.now}))


def test_write_retries_stop_at_the_write_deadline(repo):
    w = World(repo)
    w.account.inject(TARGET, "write", FaultKind.THROTTLE, times=3)

    def slow_sleep(_seconds):
        w.clock.now += timedelta(seconds=20)  # each backoff takes 20 s of wall time
    view = w.execute(sleep=slow_sleep)
    result = view.results[TARGET]
    assert result.status == ResultStatus.FAILED_WRITE and "write deadline reached" in result.detail
    assert result.attempts == 2  # the third attempt would start after the 30 s deadline
    assert [f.remaining for f in w.account._faults if f.arn == TARGET and f.op == "write"] == [1]
    assert writes_to(w, TARGET) == 0


def test_lease_and_budget_configuration_is_validated(repo):
    w = World(repo)
    with pytest.raises(ValueError, match="shorter than the lease TTL"):
        FixtureRemediationExecutor(w.svc("alice"), w.account, lease_ttl=timedelta(seconds=30))


def test_every_successful_write_is_in_the_audit_through_crashes_and_recovery(repo):
    w = World(repo)
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("after_write", TARGET))  # written, outcome not recorded
    w.clock.now += timedelta(minutes=3)
    view = w.execute(name="erin", key="key-exec-rec-01")
    assert view.summary.outcome == ProposalState.SUCCEEDED
    assert view.results[TARGET].status == ResultStatus.ALREADY_APPLIED
    assert_every_write_was_announced(w)
    intents = [e for e in view.events if e.event_type == ExecutionEventType.WRITE_INTENT]
    assert intents and all(e.write_deadline <= view.record.approval_expires_at for e in intents)


# ================================================================ M1 / L2: approval expiry

def test_resume_after_approval_expiry_is_rejected(repo):
    """Security review reproduction: a resume 5 minutes after approval expiry used to write."""
    w = World(repo)
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", TARGET))
    writes = len(w.account.write_log)
    w.clock.now = head(w).approval_expires_at + timedelta(minutes=5)  # still inside the 30-minute window
    view = w.execute(name="erin", key="key-exec-late-1")
    assert len(w.account.write_log) == writes
    assert view.results[TARGET].status == ResultStatus.NOT_ATTEMPTED
    assert "approval expired" in view.results[TARGET].detail
    assert view.summary.outcome in (ProposalState.PARTIAL, ProposalState.FAILED)


def test_execution_start_after_approval_expiry_is_rejected(repo):
    w = World(repo)
    executor = w.executor()
    expiry = head(w).approval_expires_at
    checked = executor.decisions.verify_for_execution

    def check_then_time_passes(proposal_id):
        result = checked(proposal_id)
        w.clock.now = expiry  # the approval expires between the check and the start
        return result
    executor.decisions.verify_for_execution = check_then_time_passes
    with pytest.raises(ExecutionNotAllowedError, match="APPROVAL_EXPIRED"):
        executor.execute(pid(w), approval_id=w.approval.decision_id, idempotency_key="key-exec-late-2")
    assert not w.account.write_log and not w.repo.list_executions(pid(w))


def test_start_transaction_itself_is_conditional_on_a_valid_approval(repo):
    """Even if the executor's own check were wrong, the stored head refuses the start."""
    w = World(repo)
    executor = w.executor()
    real = w.repo.get_decision
    expiry = head(w).approval_expires_at

    def lying_get_decision(proposal_id, decision_id):  # claims a later expiry than the head holds
        d = real(proposal_id, decision_id)
        return d.model_copy(update={"approval_expires_at": expiry + timedelta(hours=1)})
    executor.decisions.verify_for_execution = lambda _pid: type("ok", (), {"allowed": True, "reasons": []})()
    executor.decisions._require_current_policy = lambda *_a: None
    w.repo.get_decision = lying_get_decision
    w.clock.now = expiry + timedelta(seconds=1)
    proposal, h = w.proposal, w.repo.get_head(pid(w))  # read the head directly: no lazy expiry
    principal = executor.decisions._principal()
    with pytest.raises(ExecutionNotAllowedError, match="APPROVAL_EXPIRED"):
        identity = w.authz.resolve_identity(principal, proposal.account_id, OperatingMode.FIXTURE)
        executor._start(proposal, h, principal, identity, w.approval.decision_id, "key-exec-late-3", "fp")
    w.repo.get_decision = real
    assert not w.repo.list_executions(pid(w)) and not w.account.write_log


def test_resume_refuses_after_a_policy_change(repo, tmp_path):
    pm = PolicyManager(LocalPolicyStore(tmp_path / "pol"))
    w = World(repo, policies=pm)
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", TARGET))
    writes = len(w.account.write_log)
    data = DEFAULT_POLICY_DOCUMENT.model_dump()
    data["defaults"].append({"key": "Team"})
    pm.save(parse_policy(data), expected_version=1, created_by="admin")
    w.clock.now += timedelta(minutes=3)
    view = w.execute(name="erin", key="key-exec-pol-01")
    assert len(w.account.write_log) == writes
    assert "tagging policy changed" in view.results[TARGET].detail


# ================================================================ M1: ABORT

def test_abort_is_authorised_recorded_and_ends_the_execution(repo):
    w = World(repo)
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", TARGET))
    execution_id = head(w).execution_id
    lease_before = w.repo.get_lease(pid(w), execution_id)
    result = abort(w)
    assert result.created and result.decision.decision == DecisionType.ABORT
    assert result.decision.aborts_execution_id == execution_id and result.head.state == ProposalState.ABORTED
    assert head(w).state == ProposalState.ABORTED  # history and decisions verify on read
    view = w.executor().load(pid(w), execution_id)
    assert view.summary.outcome == ProposalState.ABORTED and view.summary.in_flight == []
    assert view.events[-1].event_type == ExecutionEventType.ABORTED
    lease = w.repo.get_lease(pid(w), execution_id)
    assert lease.released and lease.token == lease_before.token + 1
    assert abort(w).created is False  # idempotent replay with the same key
    history = w.proposals.history(pid(w))
    assert history[-1].event_type.value == "EXECUTION_ABORTED" and history[-1].actor.id == "dev:bob"


def test_unauthorised_or_mismatched_aborts_are_rejected(repo):
    w = World(repo)
    with pytest.raises(DecisionNotAllowedError, match="only EXECUTING"):
        abort(w)  # still APPROVED
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", TARGET))
    for name in ("alice", "erin", "mallory"):  # executors and strangers lack 'reject'
        with pytest.raises(AuthorizationError, match="lacks 'reject'"):
            abort(w, name=name, key=f"key-abort-{name}")
    with pytest.raises(DecisionNotAllowedError, match="does not match the running execution"):
        abort(w, execution_id="execution-00000000-0000-4000-8000-000000000000", key="key-abort-wrong")
    assert head(w).state == ProposalState.EXECUTING


def test_aborted_execution_cannot_resume_or_restart(repo):
    w = World(repo)
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", TARGET))
    abort(w)
    writes = len(w.account.write_log)
    w.clock.now += timedelta(minutes=3)
    replay = w.execute(name="alice", key="key-exec-00001")  # the original request's key: idempotent replay
    assert replay.summary.outcome == ProposalState.ABORTED
    for name, key in (("alice", "key-exec-retry1"), ("erin", "key-exec-after1")):
        with pytest.raises(ExecutionNotAllowedError, match="terminal"):
            w.execute(name=name, key=key)
    assert len(w.account.write_log) == writes


def test_abort_fences_a_running_executor_before_its_next_write(repo):
    w = World(repo)

    def abort_now(point, arn):
        if point == "before_write" and arn == TARGET and not abort_now.done:
            abort_now.done = True
            abort(w)
    abort_now.done = False
    with pytest.raises(LeaseLostError):
        w.execute(hooks=abort_now)
    assert writes_to(w, TARGET) == 0 and head(w).state == ProposalState.ABORTED


def test_a_write_already_announced_when_abort_lands_is_listed_in_flight(repo):
    """The residual race: the intent is committed, then an abort lands, then the write is issued
    (still before its deadline). It cannot be prevented - but it is never silent."""
    w = World(repo)

    def abort_now(point, arn):
        if point == "after_intent" and arn == TARGET and not abort_now.done:
            abort_now.done = True
            abort(w)
    abort_now.done = False
    with pytest.raises(LeaseLostError):
        w.execute(hooks=abort_now)
    view = w.executor().load(pid(w), head(w).execution_id)
    assert view.summary.outcome == ProposalState.ABORTED and view.summary.in_flight == [TARGET]
    assert writes_to(w, TARGET) == 1  # landed after the abort ...
    assert_every_write_was_announced(w)  # ... but announced in the audit


def test_cli_abort_requires_an_execution_id_and_identity(tmp_path, capsys):
    from cloudguard.governance.__main__ import main
    base = ["proposal", "abort", "proposal-00000000-0000-4000-8000-000000000000", "--content-hash", "sha256:x",
            "--reason", "stop", "--proposal-store", str(tmp_path / "p"), "--authz-store", str(tmp_path / "az")]
    with pytest.raises(SystemExit) as exc:
        main([*base, "--dev-identity", "bob"])  # no --execution-id
    assert exc.value.code == 2
    assert main([*base, "--dev-identity", "bob", "--execution-id",
                 "execution-00000000-0000-4000-8000-000000000000"]) == 1  # proposal not found
    assert "not found" in capsys.readouterr().err
