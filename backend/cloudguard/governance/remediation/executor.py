"""Remediation executor: the simulated account (Checkpoint 4.3; execution safety hardened in Batch 2 - M1, M2, L2)
and, since Checkpoint 5, the authenticated AWS account (LiveRemediationExecutor + live_writer.LiveTagAccount).

Flow for one execution request (proposal id + approval id + idempotency key, nothing else):
  1. identity from the IdentityProvider, in the executor's own mode (a FIXTURE executor refuses LIVE identities and
     the LIVE executor refuses development identities);
  2. load the immutable proposal and approval from server-side storage (verified on read);
  3. authorise the requester ('execute') and re-check the approval with
     DecisionService.verify_for_execution(); the current tagging policy must still be the approved
     one (else the proposal becomes STALE); the account must be the proposal's account;
  4. APPROVED -> EXECUTING with a new execution id and a lease (fencing token) in one transaction,
     conditional on the head still holding this approval, unexpired at the start instant (L2);
  5. revalidate every approved resource and record the results;
  6. per eligible resource: re-read immediately before writing; stop if the approval expired, the
     policy changed or the execution window elapsed; record a fenced WRITE_INTENT; issue the write
     only before the intent's deadline; read back and verify; record the outcome;
  7. finalise: EXECUTING -> SUCCEEDED | PARTIAL | FAILED, lease released.
An ABORT decision (DecisionService.abort) can end an EXECUTING proposal at any time (-> ABORTED).

Lease fencing (M2):
- Every audit append is a conditional write on the lease: same token, not released, and not
  expired. An expired lease is never renewed, even if nobody has taken it over yet; a takeover
  requires the old lease to have expired and bumps the token; finalisation and abort release it.
- Before every write the executor records a WRITE_INTENT. That append is the ownership check: it
  needs the lease to be valid for at least LEASE_SAFETY_MARGIN more and renews it to now + LEASE_TTL,
  so no other executor can take over before then. The write itself must be issued before the
  intent's write_deadline (now + WRITE_BUDGET, never after the approval expiry), and retries stop at
  that deadline. WRITE_BUDGET + LEASE_SAFETY_MARGIN < LEASE_TTL is enforced at construction.
- If an intent can't be recorded (takeover, finalisation, abort, expiry) the executor raises
  LeaseLostError and issues no further writes. Every write is therefore preceded by an audit record.

What this does NOT eliminate: AWS tagging APIs (TagResource, CreateTags, PutBucketTagging) accept no
fencing token. A write that has passed its last deadline check can still reach the service late if
the process pauses, the network is slow, or clocks disagree by more than LEASE_SAFETY_MARGIN. The
window is narrowed and bounded by the lease timing, not closed; the intent makes such a write
visible in the audit (in_flight / UNCONFIRMED_WRITE) rather than silent.

TOCTOU on the resource itself: a re-read immediately before each write narrows, but cannot close,
the window in which another actor changes tags. Verification after the write detects some (not all)
interference. This executor does not prove production write safety.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from cloudguard.governance.remediation.approval import (
    POLICY_UNAVAILABLE,
    DecisionNotAllowedError,
    DecisionService,
    PolicyValidationUnavailableError,
)
from cloudguard.governance.remediation.audit import in_flight_writes, verify_execution
from cloudguard.governance.remediation.execution_models import (
    ErrorCategory,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionLease,
    ExecutionRecord,
    ExecutionView,
    ResourceResult,
    ResultStatus,
    RevalidationResult,
    RevalidationStatus,
    summarize,
)
from cloudguard.governance.remediation.lifecycle import TERMINAL_STATES, event_type_for
from cloudguard.governance.remediation.models import (
    IdempotencyRecord,
    LifecycleEvent,
    OperatingMode,
    Permission,
    ProposalState,
    canonical_digest,
)
from cloudguard.governance.remediation.repository import (
    IDEMPOTENCY_RETENTION,
    ApprovalNoLongerValidError,
    IdempotencyConflictError,
    IdempotencyKeyTakenError,
    LeaseConflictError,
    TransitionConflictError,
    read_execution_state,
)
from cloudguard.governance.remediation.revalidation import revalidate
from cloudguard.governance.remediation.writers import (
    ApprovedWrite,
    FixtureAccessDenied,
    FixtureAccount,
    FixtureError,
    FixtureThrottled,
    FixtureTransient,
    FixtureWriter,
    ReadPurpose,
    ResourceSnapshot,
)

EXECUTE_SCOPE = "proposal-execute"
LEASE_TTL = timedelta(minutes=2)
WRITE_BUDGET = timedelta(seconds=30)  # a write (with its retries) must be issued within this of its intent
LEASE_SAFETY_MARGIN = timedelta(seconds=10)  # clock skew + persistence latency allowance
MAX_EXECUTION_WINDOW = timedelta(minutes=30)  # after this, a crashed execution is closed, not resumed
MAX_ATTEMPTS = 3  # per read/write, for throttling and transient server errors only
VERIFY_ATTEMPTS = 3  # read-backs allowed for eventual consistency
BACKOFF_BASE_S = 0.05

_SKIP_CATEGORY = {
    RevalidationStatus.RESOURCE_MISSING: ErrorCategory.NOT_FOUND,
    RevalidationStatus.INSPECTION_FAILED: ErrorCategory.ACCESS_DENIED,
    RevalidationStatus.UNVERIFIABLE: ErrorCategory.TRANSIENT,
}


class ExecutionNotAllowedError(PermissionError):
    pass


class LiveExecutionNotSupportedError(ExecutionNotAllowedError):
    """The fixture executor was asked to act in live mode (live execution is LiveRemediationExecutor's)."""


class ProposalStaleError(ExecutionNotAllowedError):
    pass


class ConcurrentExecutionError(RuntimeError):
    pass


class LeaseLostError(ConcurrentExecutionError):
    """This executor no longer owns the execution lease (taken over, finalised, aborted or expired).
    It has issued no write since its last recorded WRITE_INTENT and will issue none."""


class RemediationExecutor:
    """The execution flow above, for one operating mode. Use FixtureRemediationExecutor (simulated account) or
    LiveRemediationExecutor (the authenticated AWS account); this class is never used directly."""

    MODE: OperatingMode

    def _check(self, decisions: DecisionService, account) -> None:
        raise NotImplementedError

    def _writer(self, account):
        raise NotImplementedError

    def __init__(self, decisions: DecisionService, account, *,
                 clock: Callable[[], datetime] | None = None, lease_ttl: timedelta = LEASE_TTL,
                 write_budget: timedelta = WRITE_BUDGET, safety_margin: timedelta = LEASE_SAFETY_MARGIN,
                 hooks: Callable[[str, str], None] | None = None, max_attempts: int = MAX_ATTEMPTS,
                 sleep: Callable[[float], None] = lambda _s: None, instance_id: str | None = None,
                 stop_check: Callable[[], str | None] | None = None):
        self._check(decisions, account)
        if not write_budget + safety_margin < lease_ttl:
            raise ValueError("write_budget + safety_margin must be shorter than the lease TTL")
        if timedelta(seconds=sum(BACKOFF_BASE_S * 2 ** i for i in range(max_attempts - 1))) >= write_budget:
            raise ValueError("the retry backoff budget must fit inside the write budget")
        self.decisions = decisions
        self.proposals = decisions.proposals
        self.repo = decisions.proposals.repo
        self.account = account
        self.writer = self._writer(account)
        self.clock = clock or decisions.clock
        self.lease_ttl = lease_ttl
        self.write_budget = write_budget
        self.safety_margin = safety_margin
        self.hooks = hooks
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.owner = f"executor-{instance_id or uuid.uuid4()}"
        # Consulted before every write, like the approval-expiry check: a reason stops the execution cleanly (the
        # remaining resources are NOT_ATTEMPTED). Used by voice tagging's kill switch.
        self.stop_check = stop_check

    # ================================================================ public API

    def execute(self, proposal_id: str, *, approval_id: str, idempotency_key: str) -> ExecutionView:
        if self.decisions.identity.mode != self.MODE:
            raise self._mode_error()
        principal = self.decisions._principal()  # IdentityProvider; refuses mismatched modes
        fingerprint = canonical_digest({"proposal_id": proposal_id, "approval_id": approval_id,
                                        "principal": principal.canonical_id})
        idem = self.repo.get_idempotency(EXECUTE_SCOPE, idempotency_key)
        if idem is not None and (idem.request_fingerprint != fingerprint or idem.proposal_id != proposal_id):
            raise IdempotencyConflictError("idempotency key was already used for a different execution request")

        view = self.proposals.get(proposal_id)  # verified; lazy proposal/approval expiry applied
        proposal, head = view.proposal, view.head
        self.decisions._authorize(principal, proposal.account_id, Permission.EXECUTE)
        identity = self.decisions.authz.resolve_identity(principal, proposal.account_id, self.MODE)
        if self.account.account_id != proposal.account_id:
            raise ExecutionNotAllowedError(f"ACCOUNT_MISMATCH: {self.MODE.value.lower()} account {self.account.account_id} "
                                           f"is not the proposal account {proposal.account_id}")

        if idem is not None and idem.execution_id:
            existing = self.load(proposal_id, idem.execution_id)
            if existing.finished:
                return existing
        if head.state == ProposalState.EXECUTING:
            if approval_id != head.approval_id:
                raise ExecutionNotAllowedError("approval id does not match the executing approval")
            self._require_separation(identity, proposal_id, head.approval_id, proposal.account_id)
            return self._resume(proposal_id, head.execution_id, principal)
        if head.state in TERMINAL_STATES:
            raise ExecutionNotAllowedError(f"proposal is {head.state.value}; terminal proposals never execute")
        if head.state != ProposalState.APPROVED:
            raise ExecutionNotAllowedError(f"proposal is {head.state.value}, not APPROVED")
        if approval_id != head.approval_id:
            raise ExecutionNotAllowedError("approval id does not match the active approval")
        self._require_separation(identity, proposal_id, approval_id, proposal.account_id)
        check = self.decisions.verify_for_execution(proposal_id)
        if not check.allowed:
            raise ExecutionNotAllowedError("; ".join(check.reasons))
        try:
            self.decisions._require_current_policy(proposal, head)
        except PolicyValidationUnavailableError as exc:
            raise ExecutionNotAllowedError(str(exc)) from None
        except DecisionNotAllowedError as exc:
            raise ProposalStaleError(str(exc)) from None
        return self._start(proposal, head, principal, identity, approval_id, idempotency_key, fingerprint)

    def _require_separation(self, identity, proposal_id: str, approval_id: str, account_id: str) -> None:
        """M3: the executor must not be the approver (always), nor the requester when configured.
        People are compared, not principals. Checked before every start and every resume."""
        approval = self.repo.get_decision(proposal_id, approval_id)
        if approval is None:
            raise ExecutionNotAllowedError("approval record missing")
        # The one personal account a waiver names, approved under that waiver: one person does all three.
        if self.decisions.waived(account_id) and approval.separation_waived:
            return
        if identity.person_id == approval.identity.person_id:
            raise ExecutionNotAllowedError(f"separation of duties: person {identity.person_id!r} approved this "
                                           "proposal and cannot execute it")
        if self.decisions.authz.separation(account_id).executor_must_differ_from_requester(self.MODE):
            requester = self.repo.get_stored(proposal_id).requester_identity
            if requester is None:
                raise ExecutionNotAllowedError("the proposal has no requester identity evidence, so separation of "
                                               "duties can't be checked")
            if requester.person_id == identity.person_id:
                raise ExecutionNotAllowedError(f"separation of duties: person {identity.person_id!r} requested this "
                                               "proposal and cannot execute it")

    def load(self, proposal_id: str, execution_id: str) -> ExecutionView:
        stored, lifecycle, record, lease, events = read_execution_state(self.repo, proposal_id, execution_id)
        if stored is None or record is None:
            raise ExecutionNotAllowedError(f"execution {execution_id} not found")
        return verify_execution(stored, lifecycle, record, lease, events)

    def history(self, proposal_id: str) -> list[ExecutionView]:
        return [self.load(proposal_id, r.execution_id) for r in self.repo.list_executions(proposal_id)]

    # ================================================================ start / resume

    def _start(self, proposal, head, principal, identity, approval_id, idempotency_key,
               fingerprint) -> ExecutionView:
        now = self.clock()
        approval = self.repo.get_decision(proposal.proposal_id, approval_id)
        if approval is None or approval.approval_expires_at is None or not now < approval.approval_expires_at:
            raise ExecutionNotAllowedError("APPROVAL_EXPIRED: the approval expired before execution could start")
        stored = self.repo.get_stored(proposal.proposal_id)
        execution_id = f"execution-{uuid.uuid4()}"
        record = ExecutionRecord(
            execution_id=execution_id, proposal_id=proposal.proposal_id, approval_id=approval_id,
            content_hash=proposal.content_hash, binding_digest=stored.binding_digest, account_id=proposal.account_id,
            mode=self.MODE, requested_by=principal.as_actor(), approver=approval.principal, started_at=now,
            approval_expires_at=approval.approval_expires_at, idempotency_key=idempotency_key,
            executor_identity=identity, approver_identity=approval.identity,
            request_fingerprint=fingerprint, intended_resources=[r.resource_arn for r in proposal.records if r.changes],
            separation_waived=(approval.separation_waived if self.decisions.waived(proposal.account_id) else None),
        )
        lease = ExecutionLease(execution_id=execution_id, proposal_id=proposal.proposal_id, owner=self.owner, token=1,
                               acquired_at=now, expires_at=now + self.lease_ttl)
        event = LifecycleEvent(
            proposal_id=proposal.proposal_id, seq=head.seq + 1,
            event_type=event_type_for(head.state, ProposalState.EXECUTING), from_state=head.state,
            to_state=ProposalState.EXECUTING, actor=principal.as_actor(), at=now, reason="fixture execution started",
            content_hash=proposal.content_hash,
            details={"execution_id": execution_id, "approval_id": approval_id, "mode": self.MODE.value,
                     "lease_owner": self.owner, "person_id": identity.person_id},
        )
        new_head = head.model_copy(update={"state": ProposalState.EXECUTING, "seq": head.seq + 1, "updated_at": now,
                                           "execution_id": execution_id})
        idem = IdempotencyRecord(scope=EXECUTE_SCOPE, key=idempotency_key, request_fingerprint=fingerprint,
                                 proposal_id=proposal.proposal_id, created_at=now, execution_id=execution_id,
                                 ttl_epoch=int((now + IDEMPOTENCY_RETENTION).timestamp()))
        try:
            # Conditional on the head still being APPROVED with this approval, unexpired at `now` (L2).
            self.repo.start_execution(event, head, new_head, record, lease, idem)
        except IdempotencyKeyTakenError as taken:
            if taken.record.request_fingerprint != fingerprint:
                raise IdempotencyConflictError("idempotency key was already used for a different request") from None
            return self.load(proposal.proposal_id, taken.record.execution_id)
        except ApprovalNoLongerValidError:
            raise ExecutionNotAllowedError("APPROVAL_EXPIRED: the approval is no longer valid") from None
        except TransitionConflictError:
            raise ConcurrentExecutionError("another execution started first") from None
        return self._run(proposal.proposal_id, execution_id, token=1, principal=principal)

    def _resume(self, proposal_id: str, execution_id: str, principal) -> ExecutionView:
        view = self.load(proposal_id, execution_id)
        if view.finished:
            return view
        now = self.clock()
        lease = view.lease
        if lease.owner == self.owner and lease.held_at(now):
            token = lease.token
        elif lease.held_at(now):
            raise ConcurrentExecutionError(f"execution {execution_id} is in progress (lease held until "
                                           f"{lease.expires_at.isoformat()})")
        else:
            new = lease.model_copy(update={"owner": self.owner, "token": lease.token + 1, "acquired_at": now,
                                           "expires_at": now + self.lease_ttl})
            try:
                self.repo.take_over_lease(lease, new)
            except LeaseConflictError:
                raise ConcurrentExecutionError("another executor recovered this execution first") from None
            token = new.token
            self._append(view, token, ExecutionEventType.LEASE_RECOVERED, principal,
                         note=f"recovered expired lease (previous token {lease.token}, owner {lease.owner})")
            view = self.load(proposal_id, execution_id)
        stop = self._stop_reason(view.record, self.repo.get_stored(proposal_id).load(), now)
        if stop:
            return self._abandon(view, token, principal, stop)
        return self._run(proposal_id, execution_id, token=token, principal=principal)

    def _stop_reason(self, record: ExecutionRecord, proposal, now: datetime) -> str | None:
        """Why no further write may be issued for this execution, or None. Checked on resume and
        immediately before every write announcement (M1)."""
        if not now < record.approval_expires_at:
            return f"approval expired at {record.approval_expires_at.isoformat()}; remaining resources not attempted"
        if now > record.started_at + MAX_EXECUTION_WINDOW:
            return "execution window elapsed before recovery"
        if self.decisions.authz.grants(record.approver.id, record.account_id, Permission.APPROVE,
                                       self.MODE) is None:
            return "approver is no longer authorised; remaining resources not attempted"
        pm = self.proposals.policy_manager
        if pm is None:  # fail closed (L1)
            return f"{POLICY_UNAVAILABLE}; remaining resources not attempted"
        if pm.get_active(proposal.policy.policy_id).content_hash != proposal.policy.content_hash:
            return "tagging policy changed since approval; remaining resources not attempted"
        return None

    # ================================================================ the execution loop

    def _run(self, proposal_id: str, execution_id: str, *, token: int, principal) -> ExecutionView:
        view = self.load(proposal_id, execution_id)
        stored = self.repo.get_stored(proposal_id)
        proposal = stored.load()
        records = {r.resource_arn: r for r in proposal.records if r.changes}
        if self.proposals.policy_manager is None:  # fail closed (L1): no revalidation without the approved rules
            return self._abandon(view, token, principal, f"{POLICY_UNAVAILABLE}; remaining resources not attempted")
        policy = self.proposals.policy_manager.resolve(proposal.policy)
        remaining = [a for a in view.record.intended_resources if a not in view.results]

        def rules(rtype):
            return policy.document.rules_for(rtype)

        revalidated: dict[str, RevalidationResult] = {}
        for arn in remaining:
            result, _ = self._revalidate(records[arn], stored, rules, ReadPurpose.REVALIDATE)
            revalidated[arn] = result
            view = self._append(view, token, ExecutionEventType.REVALIDATED, principal, resource_arn=arn,
                                revalidation=result)

        for arn in remaining:
            record = records[arn]
            first = revalidated[arn]
            if first.status != RevalidationStatus.ELIGIBLE:
                outcome = self._not_written(first)
            else:
                self._hook("before_prewrite", arn)
                pre, snapshot = self._revalidate(record, stored, rules, ReadPurpose.PREWRITE)
                if pre.status != RevalidationStatus.ELIGIBLE:
                    outcome = self._not_written(pre, prefix="at pre-write re-read: ")
                else:
                    self._hook("before_write", arn)
                    stop = self._stop_reason(view.record, proposal, self.clock()) or (
                        self.stop_check() if self.stop_check is not None else None)
                    if stop:
                        return self._abandon(view, token, principal, stop)
                    write = ApprovedWrite(resource_arn=arn, changes=tuple(pre.pending))
                    view, deadline = self._announce(view, token, principal, write)  # raises LeaseLostError
                    self._hook("after_intent", arn)
                    outcome = self._write(record, pre, snapshot, write, deadline)
            view = self._append(view, token, ExecutionEventType.RESOURCE_RESULT, principal, resource_arn=arn,
                                result=outcome)
        return self._finalize(view, token, principal)

    def _announce(self, view: ExecutionView, token: int, principal, write: ApprovedWrite) -> tuple[ExecutionView, datetime]:
        """Record a fenced WRITE_INTENT. Succeeds only if this executor's lease is current and valid for
        at least the safety margin; renews it to now + LEASE_TTL. Returns the write deadline."""
        now = self.clock()
        deadline = min(now + self.write_budget, view.record.approval_expires_at)
        view = self._append(view, token, ExecutionEventType.WRITE_INTENT, principal, resource_arn=write.resource_arn,
                            intent_changes=list(write.changes), write_deadline=deadline,
                            valid_after=now + self.safety_margin)
        return view, deadline

    def _write(self, record, pre: RevalidationResult, snapshot: ResourceSnapshot, write: ApprovedWrite,
               deadline: datetime) -> ResourceResult:
        """Issue the write, retrying transient failures, but never at or after `deadline`."""
        request_ids = [pre.request_id] if pre.request_id else []
        attempts, last = 0, None
        while True:
            if not self.clock() < deadline:
                detail = "write deadline reached (lease safety window); not issued"
                if attempts:
                    detail += f" after {attempts} failed attempt(s): {last}"
                return ResourceResult(resource_arn=write.resource_arn, status=ResultStatus.FAILED_WRITE,
                                      revalidation=pre.status,
                                      error_category=ErrorCategory(last.category) if last else ErrorCategory.ABANDONED,
                                      detail=detail, attempts=attempts, request_ids=request_ids)
            attempts += 1
            try:
                written = self.writer.apply(write)
                break
            except (FixtureThrottled, FixtureTransient) as exc:
                last = exc
                if attempts >= self.max_attempts:
                    return ResourceResult(resource_arn=write.resource_arn, status=ResultStatus.FAILED_WRITE,
                                          revalidation=pre.status, error_category=ErrorCategory(exc.category),
                                          detail=str(exc), attempts=attempts, request_ids=request_ids)
                self.sleep(BACKOFF_BASE_S * 2 ** (attempts - 1))
            except FixtureError as exc:
                return ResourceResult(resource_arn=write.resource_arn, status=ResultStatus.FAILED_WRITE,
                                      revalidation=pre.status, error_category=ErrorCategory(exc.category),
                                      detail=str(exc), attempts=attempts, request_ids=request_ids)
        self._hook("after_write", write.resource_arn)
        return self._verify(record, pre, snapshot, write, written, attempts)

    def _not_written(self, rv: RevalidationResult, prefix: str = "") -> ResourceResult:
        if rv.status == RevalidationStatus.ALREADY_APPLIED:
            return ResourceResult(resource_arn=rv.resource_arn, status=ResultStatus.ALREADY_APPLIED,
                                  revalidation=rv.status, detail=prefix + rv.detail, attempts=rv.attempts,
                                  request_ids=[rv.request_id] if rv.request_id else [])
        return ResourceResult(resource_arn=rv.resource_arn, status=ResultStatus.SKIPPED, revalidation=rv.status,
                              error_category=_SKIP_CATEGORY.get(rv.status, ErrorCategory.INELIGIBLE),
                              detail=prefix + rv.detail, attempts=rv.attempts,
                              request_ids=[rv.request_id] if rv.request_id else [])

    def _revalidate(self, record, stored, rules, purpose: ReadPurpose) -> tuple[RevalidationResult, ResourceSnapshot | None]:
        base = dict(resource_arn=record.resource_arn, resource_type=record.resource_type, region=record.region,
                    baseline_uid=stored.resource_identities.get(record.resource_arn))
        try:
            snapshot, attempts = self._retry(lambda: self.account.read(record.resource_arn, purpose))
        except FixtureAccessDenied as exc:
            return RevalidationResult(status=RevalidationStatus.INSPECTION_FAILED, detail=str(exc), **base), None
        except (FixtureThrottled, FixtureTransient) as exc:
            return RevalidationResult(status=RevalidationStatus.UNVERIFIABLE, attempts=self.max_attempts,
                                      detail=f"{exc} (gave up after {self.max_attempts} attempts)", **base), None
        result = revalidate(record, snapshot, account_id=stored.account_id, baseline_uid=base["baseline_uid"],
                            original_rules=rules(record.resource_type), attempts=attempts)
        return result, snapshot

    def _verify(self, record, pre: RevalidationResult, snapshot: ResourceSnapshot, write: ApprovedWrite,
                written, write_attempts: int) -> ResourceResult:
        expected = {c.key: c.new_value for c in write.changes}
        request_ids = [r for r in (pre.request_id, written.request_id) if r]
        after = None
        for _ in range(VERIFY_ATTEMPTS):
            try:
                after, _attempts = self._retry(lambda: self.account.read(record.resource_arn, ReadPurpose.VERIFY,
                                                                         stale_tags=snapshot.tags))
            except FixtureError:
                after = None
                continue
            request_ids.append(after.request_id)
            if after.exists and all(after.tags.get(k) == v for k, v in expected.items()):
                break
        base = dict(resource_arn=record.resource_arn, revalidation=pre.status, attempts=write_attempts,
                    request_ids=request_ids, written_keys=sorted(expected))
        if after is None or not after.exists:
            return ResourceResult(status=ResultStatus.FAILED_VERIFICATION, error_category=ErrorCategory.VERIFICATION,
                                  detail="the resource could not be read back after the write", **base)
        if after.uid != snapshot.uid:
            return ResourceResult(status=ResultStatus.FAILED_VERIFICATION, error_category=ErrorCategory.VERIFICATION,
                                  detail="the resource identity changed during the write", **base)
        missing = sorted(k for k, v in expected.items() if after.tags.get(k) != v)
        if missing:
            return ResourceResult(status=ResultStatus.FAILED_VERIFICATION, error_category=ErrorCategory.VERIFICATION,
                                  detail=f"approved values not present after write: {', '.join(missing)}", **base)
        warnings = []
        for k, v in (snapshot.tags or {}).items():
            if k not in expected and after.tags.get(k) != v:
                warnings.append(f"unrelated tag {k!r} changed during execution (concurrent modification)")
        for k in after.tags:
            if k not in expected and k not in (snapshot.tags or {}):
                warnings.append(f"tag {k!r} appeared during execution (concurrent modification)")
        return ResourceResult(status=ResultStatus.APPLIED_VERIFIED, detail="approved values verified after write",
                              warnings=warnings, **base)

    # ================================================================ helpers

    def _retry(self, fn):
        """Bounded retries for READS only (writes go through _write, which also honours the deadline)."""
        attempts = 0
        while True:
            attempts += 1
            try:
                return fn(), attempts
            except (FixtureThrottled, FixtureTransient):
                if attempts >= self.max_attempts:
                    raise
                self.sleep(BACKOFF_BASE_S * 2 ** (attempts - 1))

    def _hook(self, point: str, arn: str) -> None:
        if self.hooks is not None:
            self.hooks(point, arn)

    def _append(self, view: ExecutionView, token: int, event_type: ExecutionEventType, principal, *,
                valid_after: datetime | None = None, **payload) -> ExecutionView:
        now = self.clock()
        prev = view.events[-1].event_hash if view.events else view.record.record_hash()
        event = ExecutionEvent.sealed(
            execution_id=view.record.execution_id, proposal_id=view.record.proposal_id, seq=len(view.events) + 1,
            event_type=event_type, at=now, lease_token=token, actor=principal.as_actor(), prev_hash=prev, **payload)
        try:
            self.repo.append_execution_event(event, token, now + self.lease_ttl, valid_after or now)
        except LeaseConflictError as exc:
            raise LeaseLostError(f"lease lost; no further writes will be issued: {exc}") from None
        results = dict(view.results)
        if event.result is not None:
            results[event.resource_arn] = event.result
        return view.model_copy(update={"events": [*view.events, event], "results": results})

    def _abandon(self, view: ExecutionView, token: int, principal, reason: str) -> ExecutionView:
        in_flight = set(in_flight_writes(view.events))
        for arn in view.record.intended_resources:
            if arn in view.results:
                continue
            if arn in in_flight:  # an announced write whose outcome was never recorded
                result = ResourceResult(resource_arn=arn, status=ResultStatus.UNCONFIRMED_WRITE, revalidation=None,
                                        error_category=ErrorCategory.ABANDONED,
                                        detail=f"write intent recorded but outcome unknown; {reason}")
            else:
                result = ResourceResult(resource_arn=arn, status=ResultStatus.NOT_ATTEMPTED, revalidation=None,
                                        error_category=ErrorCategory.ABANDONED, detail=reason)
            view = self._append(view, token, ExecutionEventType.RESOURCE_RESULT, principal, resource_arn=arn,
                                result=result)
        return self._finalize(view, token, principal)

    def _finalize(self, view: ExecutionView, token: int, principal) -> ExecutionView:
        now = self.clock()
        record = view.record
        summary = summarize(record.intended_resources, view.results)
        prev = view.events[-1].event_hash if view.events else record.record_hash()
        final = ExecutionEvent.sealed(execution_id=record.execution_id, proposal_id=record.proposal_id,
                                      seq=len(view.events) + 1, event_type=ExecutionEventType.FINALIZED, at=now,
                                      lease_token=token, actor=principal.as_actor(), summary=summary, prev_hash=prev)
        head = self.repo.get_head(record.proposal_id)
        if head is None or head.state != ProposalState.EXECUTING or head.execution_id != record.execution_id:
            raise LeaseLostError("proposal is no longer executing this execution")
        event = LifecycleEvent(
            proposal_id=record.proposal_id, seq=head.seq + 1, event_type=event_type_for(head.state, summary.outcome),
            from_state=head.state, to_state=summary.outcome, actor=principal.as_actor(), at=now,
            reason=f"fixture execution {summary.outcome.value.lower()}", content_hash=record.content_hash,
            details={"execution_id": record.execution_id, "intended": summary.intended, "succeeded": summary.succeeded,
                     "already_applied": summary.already_applied, "skipped": summary.skipped,
                     "failed": summary.failed, "not_attempted": summary.not_attempted},
        )
        new_head = head.model_copy(update={"state": summary.outcome, "seq": head.seq + 1, "updated_at": now})
        try:
            self.repo.finish_execution(event, head, new_head, final, token)
        except LeaseConflictError as exc:
            raise LeaseLostError(f"lease lost before finalisation: {exc}") from None
        return self.load(record.proposal_id, record.execution_id)


class FixtureRemediationExecutor(RemediationExecutor):
    """Executes against the simulated FixtureAccount only, with FIXTURE identities."""

    MODE = OperatingMode.FIXTURE

    def _check(self, decisions, account) -> None:
        if decisions.mode != OperatingMode.FIXTURE:
            raise LiveExecutionNotSupportedError("the fixture executor never runs in live mode; use "
                                                 "LiveRemediationExecutor with a LiveTagAccount")
        if type(account) is not FixtureAccount:
            raise TypeError("the fixture executor only operates on a FixtureAccount")

    def _writer(self, account):
        return FixtureWriter(account)

    def _mode_error(self):
        return LiveExecutionNotSupportedError("the fixture executor never runs with a live identity")


class LiveRemediationExecutor(RemediationExecutor):
    """Executes an approved proposal against the authenticated AWS account (Checkpoint 5).

    Only with LIVE operating mode and an STS-verified LIVE identity, which in turn means the approval was
    made by a different, allow-listed person (separation of duties is enforced by DecisionService and
    _require_separation). There is no flag or environment switch: the caller constructs this class with a
    LiveTagAccount explicitly, which only the CLI's `proposal execute --mode live` does."""

    MODE = OperatingMode.LIVE

    def _check(self, decisions, account) -> None:
        from cloudguard.governance.remediation.live_writer import LiveTagAccount
        if decisions.mode != OperatingMode.LIVE:
            raise ExecutionNotAllowedError("the live executor needs LIVE operating mode and identities")
        if type(account) is not LiveTagAccount:
            raise TypeError("the live executor only operates on a LiveTagAccount")

    def _writer(self, account):
        from cloudguard.governance.remediation.live_writer import LiveWriter
        return LiveWriter(account)

    def _mode_error(self):
        return ExecutionNotAllowedError("the live executor needs an STS-verified LIVE identity")
