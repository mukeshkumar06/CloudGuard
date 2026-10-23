"""Authenticated, server-side approval decisions (Checkpoint 4.2).

DecisionService is the only way to create, approve, reject or revoke a stored proposal.
- Identity comes from the IdentityProvider it was built with, never from an argument. No method
  accepts a principal, a display name or free text that claims a decision: an AI- or
  user-written "approved" string has no path to authorisation.
- Every decision is bound to the exact proposal id and content hash, the authenticated principal,
  the authorisation basis (allow-list version + matched entry), a single-use nonce and an
  idempotency key. It is written atomically with its lifecycle event (append-only).
- LIVE mode: STS-authenticated principals only, target account must match the caller's account,
  and the approver must differ from an authenticated requester. FIXTURE mode allows development
  identities (including self-approval) and can never authorise live AWS operations.
- M3: every action resolves the principal to a PERSON through the allow-list's person bindings
  (unresolved principals fail closed) and records IdentityEvidence. Separation of duties compares
  people, not principals: in LIVE the approver must differ from the requester (also in FIXTURE when
  fixture_approver_must_differ_from_requester is set; off by default, so fixture self-approval works).
- Personal single-operator accounts: a DecisionService built with a PersonalAccountWaiver for one account (only
  cloudguard.voice_tagging does, after checking its enable flag, its account allow-list and the STS caller) lets the
  one allow-listed person request, approve and execute in that account. Everything else still applies (identity,
  allow-list permissions, content hash, nonce, overwrite acknowledgement, expiry, policy, account match), and the
  decision records that the waiver was used. No CLI path builds a waiver, so team and production accounts keep
  full separation of duties.
- ABORT (Batch 2 / M1) stops an EXECUTING proposal: it needs the 'reject' permission, is bound to
  the running execution id, revokes the execution lease (new token, released) so no executor can
  record or announce another write, and closes the execution's audit chain - all in one write.

No execution happens here; verify_for_execution() is the re-check a future executor must call.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from cloudguard.governance.remediation.audit import in_flight_writes, verify_execution
from cloudguard.governance.remediation.authz import AuthzManager
from cloudguard.governance.remediation.execution_models import ExecutionEvent, ExecutionEventType, summarize
from cloudguard.governance.remediation.identity import IdentityError, IdentityProvider, require_identity_for_mode
from cloudguard.governance.remediation.lifecycle import event_type_for
from cloudguard.governance.remediation.models import (
    SYSTEM_EXPIRY_ACTOR,
    ActorType,
    CreateResult,
    DecisionRecord,
    DecisionType,
    IdempotencyRecord,
    LifecycleEvent,
    NonceRecord,
    OperatingMode,
    Permission,
    ProposalHead,
    ProposalState,
    canonical_digest,
)
from cloudguard.governance.remediation.repository import (
    IDEMPOTENCY_RETENTION,
    IdempotencyConflictError,
    IdempotencyKeyTakenError,
    ProposalManager,
    read_execution_state,
)

APPROVAL_TTL = timedelta(minutes=15)
DECISION_SCOPE = "proposal-decision"
NONCE_RETENTION = timedelta(days=7)  # outlives every approval (<= 15 min) it protects


class PersonalAccountWaiver(BaseModel):
    """Separation of duties waived for exactly one personal development account, in one mode (see the docstring)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    mode: OperatingMode
    reason: str = "personal single-operator voice tagging"

    def covers(self, account_id: str, mode: OperatingMode) -> bool:
        return account_id == self.account_id and mode == self.mode


class AuthorizationError(PermissionError):
    pass


class DecisionNotAllowedError(PermissionError):
    """The proposal's state, content or requirements don't permit this decision."""


class PolicyValidationUnavailableError(DecisionNotAllowedError):
    """No policy manager is available, so the policy can't be validated: fail closed (Batch 2.1 / L1)."""


POLICY_UNAVAILABLE = "policy validation is unavailable (no policy manager); refusing rather than skipping the check"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DecisionResult(_Frozen):
    decision: DecisionRecord
    head: ProposalHead
    created: bool  # False when an identical request was replayed with the same idempotency key


class ExecutionPreconditions(_Frozen):
    """Outcome of the authorisation re-check a future executor must perform. Not an execution."""

    allowed: bool
    reasons: list[str]
    decision_id: str | None = None


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


class DecisionService:
    def __init__(self, proposals: ProposalManager, authz: AuthzManager, identity: IdentityProvider,
                 clock: Callable[[], datetime] | None = None, waiver: PersonalAccountWaiver | None = None):
        if proposals.policy_manager is None:  # L1: decisions and executions must always validate the policy
            raise ValueError("DecisionService requires a ProposalManager with a policy_manager; "
                             "policy validation can't be skipped")
        self.proposals = proposals
        self.authz = authz
        self.identity = identity
        self.mode = identity.mode
        self.clock = clock or proposals.clock
        self.waiver = waiver

    def waived(self, account_id: str) -> bool:
        """True only for the one personal account this service was built for (never by default)."""
        return self.waiver is not None and self.waiver.covers(account_id, self.mode)

    def _principal(self):
        principal = self.identity.current()
        require_identity_for_mode(principal, self.mode)
        return principal

    def _authorize(self, principal, account_id: str, permission: Permission):
        if self.mode == OperatingMode.LIVE and principal.account_id != account_id:
            raise AuthorizationError(f"caller account {principal.account_id} is not the proposal account {account_id}")
        decision = self.authz.evaluate(principal, account_id, permission, self.mode)
        if not decision.allowed:
            raise AuthorizationError(decision.reason)
        return decision.basis

    # ------------------------------------------------------------------ create

    def create_proposal(self, proposal, *, idempotency_key: str,
                        resource_identities: dict[str, str] | None = None) -> CreateResult:
        principal = self._principal()
        self._authorize(principal, proposal.account_id, Permission.CREATE)
        identity = self.authz.resolve_identity(principal, proposal.account_id, self.mode)  # M3: fail closed
        return self.proposals.save(proposal, idempotency_key=idempotency_key, requested_by=principal.as_actor(),
                                   resource_identities=resource_identities, requester_identity=identity)

    # ------------------------------------------------------------------ decisions

    def approve(self, proposal_id: str, *, content_hash: str, nonce: str, idempotency_key: str,
                acknowledge_overwrites: bool = False, reason: str = "") -> DecisionResult:
        return self._decide(DecisionType.APPROVE, proposal_id, content_hash=content_hash, nonce=nonce,
                            idempotency_key=idempotency_key, acknowledge_overwrites=acknowledge_overwrites, reason=reason)

    def reject(self, proposal_id: str, *, content_hash: str, nonce: str, idempotency_key: str, reason: str) -> DecisionResult:
        return self._decide(DecisionType.REJECT, proposal_id, content_hash=content_hash, nonce=nonce,
                            idempotency_key=idempotency_key, reason=reason)

    def revoke(self, proposal_id: str, *, content_hash: str, approval_id: str, nonce: str, idempotency_key: str,
               reason: str) -> DecisionResult:
        return self._decide(DecisionType.REVOKE, proposal_id, content_hash=content_hash, nonce=nonce,
                            idempotency_key=idempotency_key, reason=reason, approval_id=approval_id)

    def abort(self, proposal_id: str, *, content_hash: str, execution_id: str, nonce: str, idempotency_key: str,
              reason: str) -> DecisionResult:
        """Stop an EXECUTING proposal (EXECUTING -> ABORTED). Writes already announced by a WRITE_INTENT
        may still land (no tagging API accepts a fencing token); they are listed as in_flight."""
        principal = self._principal()
        fingerprint = canonical_digest({
            "decision": DecisionType.ABORT.value, "proposal_id": proposal_id, "content_hash": content_hash,
            "principal": principal.canonical_id, "execution_id": execution_id, "reason": reason,
        })
        replay = self._replay(idempotency_key, fingerprint, proposal_id)
        if replay is not None:
            return replay

        view = self.proposals.get(proposal_id)
        proposal, head = view.proposal, view.head
        now = self.clock()
        if content_hash != proposal.content_hash:
            raise DecisionNotAllowedError("content hash does not match the stored proposal")
        basis = self._authorize(principal, proposal.account_id, Permission.REJECT)
        identity = self.authz.resolve_identity(principal, proposal.account_id, self.mode)  # M3: fail closed
        if head.state != ProposalState.EXECUTING:
            raise DecisionNotAllowedError(f"only EXECUTING proposals can be aborted (state is {head.state.value})")
        if execution_id != head.execution_id:
            raise DecisionNotAllowedError("execution id does not match the running execution")
        repo = self.proposals.repo
        stored, lifecycle, record, lease, events = read_execution_state(repo, proposal_id, execution_id)
        if stored is None or record is None or record.mode != self.mode:
            raise DecisionNotAllowedError("an execution can only be aborted in the mode it runs in")
        execution = verify_execution(stored, lifecycle, record, lease, events)
        if execution.finished:
            raise DecisionNotAllowedError("the execution has already finished")

        decision = DecisionRecord(
            decision_id=f"decision-{uuid.uuid4()}", decision=DecisionType.ABORT, proposal_id=proposal_id,
            content_hash=proposal.content_hash, binding_digest=self._binding(proposal_id),
            principal=principal.as_actor(), principal_arn=principal.caller_arn, authorization=basis, mode=self.mode,
            overwrite_acknowledged=False, nonce=nonce, idempotency_key=idempotency_key, request_fingerprint=fingerprint,
            decided_at=now, approval_expires_at=None, aborts_execution_id=execution_id, reason=reason,
            identity=identity,
        )
        in_flight = in_flight_writes(execution.events)
        new_lease = lease.model_copy(update={"owner": f"abort:{decision.decision_id}", "token": lease.token + 1,
                                             "acquired_at": now, "expires_at": now, "released": True})
        prev = execution.events[-1].event_hash if execution.events else record.record_hash()
        abort_event = ExecutionEvent.sealed(
            execution_id=execution_id, proposal_id=proposal_id, seq=len(execution.events) + 1,
            event_type=ExecutionEventType.ABORTED, at=now, lease_token=new_lease.token, actor=principal.as_actor(),
            summary=summarize(record.intended_resources, execution.results, aborted=True, in_flight=in_flight),
            note=reason or "aborted", prev_hash=prev)
        event = LifecycleEvent(
            proposal_id=proposal_id, seq=head.seq + 1, event_type=event_type_for(head.state, ProposalState.ABORTED),
            from_state=head.state, to_state=ProposalState.ABORTED, actor=principal.as_actor(), at=now,
            reason=reason or "abort", content_hash=proposal.content_hash,
            details={"decision_id": decision.decision_id, "execution_id": execution_id, "mode": self.mode.value,
                     "authz_config": basis.ref.config_id, "authz_version": basis.ref.version,
                     "authz_hash": basis.ref.content_hash, "matched_entry": basis.matched_entry,
                     "in_flight_writes": len(in_flight), "person_id": identity.person_id,
                     "binding_kind": identity.binding_kind},
        )
        new_head = head.model_copy(update={"state": ProposalState.ABORTED, "seq": head.seq + 1, "updated_at": now})
        nonce_record = NonceRecord(nonce=nonce, proposal_id=proposal_id, decision_id=decision.decision_id, used_at=now,
                                   ttl_epoch=int((now + NONCE_RETENTION).timestamp()))
        idem = IdempotencyRecord(scope=DECISION_SCOPE, key=idempotency_key, request_fingerprint=fingerprint,
                                 proposal_id=proposal_id, decision_id=decision.decision_id, created_at=now,
                                 ttl_epoch=int((now + IDEMPOTENCY_RETENTION).timestamp()))
        try:
            repo.record_abort(event, head, new_head, decision, nonce_record, idem, abort_event, lease, new_lease)
        except IdempotencyKeyTakenError:
            replay = self._replay(idempotency_key, fingerprint, proposal_id)
            if replay is None:  # pragma: no cover - the record exists by definition
                raise
            return replay
        return DecisionResult(decision=decision, head=new_head, created=True)

    def _decide(self, kind: DecisionType, proposal_id: str, *, content_hash: str, nonce: str, idempotency_key: str,
                reason: str, acknowledge_overwrites: bool = False, approval_id: str | None = None) -> DecisionResult:
        principal = self._principal()
        fingerprint = canonical_digest({
            "decision": kind.value, "proposal_id": proposal_id, "content_hash": content_hash,
            "principal": principal.canonical_id, "acknowledge_overwrites": acknowledge_overwrites,
            "approval_id": approval_id, "reason": reason,
        })
        replay = self._replay(idempotency_key, fingerprint, proposal_id)
        if replay is not None:
            return replay

        view = self.proposals.get(proposal_id)  # verifies stored proposal, history, decisions; lazy expiry
        proposal, head, requester = view.proposal, view.head, view.created_by
        now = self.clock()
        if content_hash != proposal.content_hash:
            raise DecisionNotAllowedError("content hash does not match the stored proposal")

        permission = Permission.APPROVE if kind == DecisionType.APPROVE else Permission.REJECT
        basis = self._authorize(principal, proposal.account_id, permission)
        identity = self.authz.resolve_identity(principal, proposal.account_id, self.mode)  # M3: fail closed

        if kind == DecisionType.REVOKE:
            if head.state != ProposalState.APPROVED:
                raise DecisionNotAllowedError(f"only APPROVED proposals can be revoked (state is {head.state.value})")
            if approval_id != head.approval_id:
                raise DecisionNotAllowedError("approval id does not match the active approval")
            to_state = ProposalState.REVOKED
        else:
            if head.state != ProposalState.PENDING:
                raise DecisionNotAllowedError(f"proposal is {head.state.value}, not PENDING")
            to_state = ProposalState.APPROVED if kind == DecisionType.APPROVE else ProposalState.REJECTED

        approval_expires_at = None
        if kind == DecisionType.APPROVE:
            if not proposal.approvable_at(now):
                raise DecisionNotAllowedError("proposal is not approvable now (expired or blocked)")
            if proposal.requires_overwrite_confirmation and not acknowledge_overwrites:
                raise DecisionNotAllowedError(
                    f"proposal overwrites {proposal.impact.tags_overwritten} existing tag value(s); "
                    "approval requires explicit overwrite acknowledgement")
            if self.mode == OperatingMode.LIVE:
                if requester.actor_type != ActorType.AWS_PRINCIPAL:
                    raise DecisionNotAllowedError("LIVE approval requires a proposal created by an authenticated principal")
                # M3: separation of duties compares PEOPLE (below), not principals. One principal resolves
                # to one person, except a session_attributed_role, which exists to tell people apart.
            separate = (self.mode == OperatingMode.LIVE
                        or self.authz.separation(proposal.account_id).fixture_approver_must_differ_from_requester)
            if separate and not self.waived(proposal.account_id):  # M3: compare PEOPLE, not principals
                requester_identity = self.proposals.repo.get_stored(proposal_id).requester_identity
                if requester_identity is None:
                    raise DecisionNotAllowedError("the proposal has no requester identity evidence, so separation "
                                                  "of duties can't be checked")
                if requester_identity.person_id == identity.person_id:
                    raise DecisionNotAllowedError(f"separation of duties: person {identity.person_id!r} requested this "
                                                  "proposal and cannot approve it")
            self._require_current_policy(proposal, head)
            approval_expires_at = min(now + APPROVAL_TTL, proposal.expires_at)

        decision = DecisionRecord(
            decision_id=f"decision-{uuid.uuid4()}", decision=kind, proposal_id=proposal_id,
            content_hash=proposal.content_hash, binding_digest=self._binding(proposal_id),
            principal=principal.as_actor(), principal_arn=principal.caller_arn, authorization=basis, mode=self.mode,
            overwrite_acknowledged=acknowledge_overwrites, nonce=nonce, idempotency_key=idempotency_key,
            request_fingerprint=fingerprint, decided_at=now, approval_expires_at=approval_expires_at,
            revokes_decision_id=approval_id if kind == DecisionType.REVOKE else None, reason=reason,
            identity=identity,
            separation_waived=(self.waiver.reason if kind == DecisionType.APPROVE and self.waived(proposal.account_id)
                               else None),
        )
        event = LifecycleEvent(
            proposal_id=proposal_id, seq=head.seq + 1, event_type=event_type_for(head.state, to_state),
            from_state=head.state, to_state=to_state, actor=principal.as_actor(), at=now,
            reason=reason or kind.value.lower(), content_hash=proposal.content_hash,
            details={
                "decision_id": decision.decision_id, "mode": self.mode.value,
                "authz_config": basis.ref.config_id, "authz_version": basis.ref.version,
                "authz_hash": basis.ref.content_hash, "matched_entry": basis.matched_entry,
                "overwrite_acknowledged": acknowledge_overwrites,
                "approval_expires_at": approval_expires_at.isoformat() if approval_expires_at else None,
                "revokes_decision_id": decision.revokes_decision_id,
                "person_id": identity.person_id, "binding_kind": identity.binding_kind,
                **({"separation_waived": self.waiver.reason} if self.waived(proposal.account_id) else {}),
            },
        )
        update = {"state": to_state, "seq": head.seq + 1, "updated_at": now}
        if kind == DecisionType.APPROVE:
            update |= {"approval_id": decision.decision_id, "approval_expires_at": approval_expires_at}
        new_head = head.model_copy(update=update)
        nonce_record = NonceRecord(nonce=nonce, proposal_id=proposal_id, decision_id=decision.decision_id, used_at=now,
                                   ttl_epoch=int(((approval_expires_at or now) + NONCE_RETENTION).timestamp()))
        idem = IdempotencyRecord(scope=DECISION_SCOPE, key=idempotency_key, request_fingerprint=fingerprint,
                                 proposal_id=proposal_id, decision_id=decision.decision_id, created_at=now,
                                 ttl_epoch=int((now + IDEMPOTENCY_RETENTION).timestamp()))
        try:
            self.proposals.repo.record_decision(event, head, new_head, decision, nonce_record, idem)
        except IdempotencyKeyTakenError:
            replay = self._replay(idempotency_key, fingerprint, proposal_id)
            if replay is None:  # pragma: no cover - the record exists by definition
                raise
            return replay
        return DecisionResult(decision=decision, head=new_head, created=True)

    def _binding(self, proposal_id: str) -> str:
        stored = self.proposals.repo.get_stored(proposal_id)
        return stored.binding_digest

    def _replay(self, key: str, fingerprint: str, proposal_id: str) -> DecisionResult | None:
        record = self.proposals.repo.get_idempotency(DECISION_SCOPE, key)
        if record is None:
            return None
        if record.request_fingerprint != fingerprint or record.proposal_id != proposal_id:
            raise IdempotencyConflictError("idempotency key was already used for a different decision")
        decision = self.proposals.repo.get_decision(proposal_id, record.decision_id)
        if decision is None:
            raise IdempotencyConflictError("idempotency record references a decision that does not exist")
        return DecisionResult(decision=decision, head=self.proposals.get(proposal_id).head, created=False)

    def _require_current_policy(self, proposal, head: ProposalHead) -> None:
        """Approving against a superseded policy is refused, and the proposal is marked STALE."""
        pm = self.proposals.policy_manager
        if pm is None:  # fail closed (L1): never treat a missing policy manager as "policy unchanged"
            raise PolicyValidationUnavailableError(POLICY_UNAVAILABLE)
        active = pm.get_active(proposal.policy.policy_id)
        if active.content_hash == proposal.policy.content_hash:
            return
        now = self.clock()
        event = LifecycleEvent(
            proposal_id=proposal.proposal_id, seq=head.seq + 1, event_type=event_type_for(head.state, ProposalState.STALE),
            from_state=head.state, to_state=ProposalState.STALE, actor=SYSTEM_EXPIRY_ACTOR, at=now,
            reason="tagging policy changed since the proposal was created", content_hash=proposal.content_hash,
            details={"proposal_policy_version": proposal.policy.version, "active_policy_version": active.version},
        )
        self.proposals.repo.append_transition(
            event, head, head.model_copy(update={"state": ProposalState.STALE, "seq": head.seq + 1, "updated_at": now}))
        raise DecisionNotAllowedError("tagging policy changed since the proposal was created; it is now STALE")

    # ------------------------------------------------------------------ re-check for future execution

    def verify_for_execution(self, proposal_id: str) -> ExecutionPreconditions:
        """Re-check, at the moment of a future execution, that the approval still authorises it.
        Performs no AWS writes and does not execute anything."""
        reasons: list[str] = []
        try:
            caller = self._principal()
        except IdentityError as exc:
            return ExecutionPreconditions(allowed=False, reasons=[str(exc)])
        view = self.proposals.get(proposal_id)  # applies approval/proposal expiry first
        head, proposal = view.head, view.proposal
        if head.state != ProposalState.APPROVED or head.approval_id is None:
            return ExecutionPreconditions(allowed=False, reasons=[f"proposal is {head.state.value}, not APPROVED"])
        approval = self.proposals.repo.get_decision(proposal_id, head.approval_id)
        now = self.clock()
        checks = [
            (approval is not None, "approval record missing"),
            (approval is not None and approval.decision == DecisionType.APPROVE, "decision is not an approval"),
            (approval is not None and approval.content_hash == proposal.content_hash, "approval content hash mismatch"),
            (approval is not None and approval.approval_expires_at is not None and now < approval.approval_expires_at,
             "approval expired"),
            (proposal.approvable_at(now), "proposal expired"),
            (approval is not None and approval.mode == self.mode, "approval was made in a different mode"),
        ]
        reasons += [msg for ok, msg in checks if not ok]
        if self.mode == OperatingMode.LIVE and caller.account_id != proposal.account_id:
            reasons.append("caller account does not match the proposal account")
        if approval is not None and approval.mode == OperatingMode.FIXTURE and self.mode == OperatingMode.LIVE:
            reasons.append("FIXTURE approvals never authorise LIVE operations")
        if approval is not None and not self.authz.grants(approval.principal.id, proposal.account_id,
                                                          Permission.APPROVE, approval.mode):
            reasons.append("approver is no longer authorised under the active allow-list")
        if approval is not None and proposal.requires_overwrite_confirmation and not approval.overwrite_acknowledged:
            reasons.append("overwrite acknowledgement missing")
        return ExecutionPreconditions(allowed=not reasons, reasons=reasons,
                                      decision_id=approval.decision_id if approval else None)
