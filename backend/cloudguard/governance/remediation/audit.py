"""Audit history: reconstruct and verify proposal state from the append-only event stream.

The event stream is authoritative. ProposalHead is a materialised view that must always agree
with what the events say; any disagreement is reported, never silently trusted.
"""

from __future__ import annotations

from datetime import datetime

from cloudguard.governance.remediation.execution_models import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionIntegrityError,
    ExecutionLease,
    ExecutionRecord,
    ExecutionView,
    summarize,
)
from cloudguard.governance.remediation.lifecycle import IllegalTransitionError, replay
from cloudguard.governance.remediation.models import (
    DecisionRecord,
    DecisionType,
    EventType,
    LifecycleEvent,
    ProposalHead,
    ProposalIntegrityError,
    ProposalState,
    StoredProposal,
)


def reconstruct_head(stored: StoredProposal, events: list[LifecycleEvent]) -> ProposalHead:
    """Derive the current head purely from the stored proposal and its events."""
    if not events:
        raise ProposalIntegrityError(f"proposal {stored.proposal_id} has no lifecycle events")
    for e in events:
        if e.proposal_id != stored.proposal_id or e.content_hash != stored.content_hash:
            raise ProposalIntegrityError(f"event {e.seq} does not belong to proposal {stored.proposal_id}")
    requester = stored.requester_identity.person_id if stored.requester_identity else None
    if events[0].details.get("requester_person") != requester:  # M3: requester evidence vs creation event
        raise ProposalIntegrityError(f"proposal {stored.proposal_id}: requester identity does not match its creation event")
    try:
        state, seq = replay(events)
    except IllegalTransitionError as exc:
        raise ProposalIntegrityError(f"proposal {stored.proposal_id} has an invalid event history: {exc}") from None
    approval_id = approval_expires_at = execution_id = None
    for e in events:
        if e.event_type == EventType.EXECUTION_STARTED:
            execution_id = str(e.details.get("execution_id") or "") or None
            if execution_id is None:
                raise ProposalIntegrityError(f"EXECUTION_STARTED event {e.seq} lacks an execution id")
        if e.event_type == EventType.APPROVED:
            try:
                approval_id = str(e.details["decision_id"])
                approval_expires_at = datetime.fromisoformat(str(e.details["approval_expires_at"]))
            except (KeyError, ValueError):
                raise ProposalIntegrityError(f"APPROVED event {e.seq} lacks approval details") from None
    return ProposalHead(
        proposal_id=stored.proposal_id,
        state=state,
        seq=seq,
        content_hash=stored.content_hash,
        proposal_expires_at=stored.proposal_expires_at,
        updated_at=events[-1].at,
        approval_id=approval_id,
        approval_expires_at=approval_expires_at,
        execution_id=execution_id,
    )


_DECISION_EVENTS = {EventType.APPROVED: DecisionType.APPROVE, EventType.REJECTED: DecisionType.REJECT,
                    EventType.REVOKED: DecisionType.REVOKE, EventType.EXECUTION_ABORTED: DecisionType.ABORT}


def verify_decisions(stored: StoredProposal, events: list[LifecycleEvent], decisions: list[DecisionRecord]) -> None:
    """Every decision event must reference exactly one matching decision record, and every decision
    record must be referenced by exactly one event. Detects tampered, orphaned or forged decisions."""
    by_id = {d.decision_id: d for d in decisions}
    if len(by_id) != len(decisions):
        raise ProposalIntegrityError(f"proposal {stored.proposal_id} has duplicate decision records")
    referenced: set[str] = set()
    for e in events:
        expected = _DECISION_EVENTS.get(e.event_type)
        if expected is None:
            continue
        decision_id = e.details.get("decision_id")
        d = by_id.get(str(decision_id))
        if d is None:
            raise ProposalIntegrityError(f"event {e.seq} references missing decision {decision_id}")
        if decision_id in referenced:
            raise ProposalIntegrityError(f"decision {decision_id} is referenced twice")
        referenced.add(str(decision_id))
        mismatches = [name for name, ok in (
            ("decision", d.decision == expected),
            ("proposal", d.proposal_id == stored.proposal_id),
            ("content_hash", d.content_hash == stored.content_hash == e.content_hash),
            ("binding_digest", d.binding_digest == stored.binding_digest),
            ("actor", d.principal == e.actor),
            ("time", d.decided_at == e.at),
            ("person", e.details.get("person_id") == d.identity.person_id
             and e.details.get("binding_kind") == d.identity.binding_kind),
            ("approval_expiry", d.decision != DecisionType.APPROVE
             or e.details.get("approval_expires_at") == d.approval_expires_at.isoformat()),
            ("execution", d.decision != DecisionType.ABORT
             or e.details.get("execution_id") == d.aborts_execution_id),
        ) if not ok]
        if mismatches:
            raise ProposalIntegrityError(f"decision {decision_id} does not match event {e.seq}: {', '.join(mismatches)}")
    orphans = set(by_id) - referenced
    if orphans:
        raise ProposalIntegrityError(f"decision records without events: {sorted(orphans)}")


def verify_head(head: ProposalHead, stored: StoredProposal, events: list[LifecycleEvent]) -> ProposalHead:
    """Return the reconstructed head, or raise if the stored head contradicts the events.

    A head that is merely *behind* the events (a crash between appending an event and updating
    the head) is not an integrity failure: the caller may repair it from the returned value.
    """
    derived = reconstruct_head(stored, events)
    if head.seq > derived.seq:
        raise ProposalIntegrityError(f"head of {stored.proposal_id} is ahead of its event history")
    if head.seq == derived.seq and (head.state, head.content_hash, head.approval_id, head.approval_expires_at,
                                    head.execution_id) != \
            (derived.state, derived.content_hash, derived.approval_id, derived.approval_expires_at,
             derived.execution_id):
        raise ProposalIntegrityError(f"head of {stored.proposal_id} contradicts its event history")
    return derived


_EXECUTION_END = {EventType.EXECUTION_SUCCEEDED: ProposalState.SUCCEEDED, EventType.EXECUTION_PARTIAL: ProposalState.PARTIAL,
                  EventType.EXECUTION_FAILED: ProposalState.FAILED, EventType.EXECUTION_ABORTED: ProposalState.ABORTED}
_CLOSING = (ExecutionEventType.FINALIZED, ExecutionEventType.ABORTED)


def in_flight_writes(events: list[ExecutionEvent]) -> list[str]:
    """Resources whose latest WRITE_INTENT has no later RESOURCE_RESULT: a write may have been issued,
    but its outcome was never recorded."""
    last_intent: dict[str, int] = {}
    last_result: dict[str, int] = {}
    for e in events:
        if e.event_type == ExecutionEventType.WRITE_INTENT:
            last_intent[e.resource_arn] = e.seq
        elif e.event_type == ExecutionEventType.RESOURCE_RESULT:
            last_result[e.resource_arn] = e.seq
    return sorted(a for a, seq in last_intent.items() if seq > last_result.get(a, 0))


def verify_execution(stored: StoredProposal, lifecycle: list[LifecycleEvent], record: ExecutionRecord,
                     lease: ExecutionLease | None, events: list[ExecutionEvent]) -> ExecutionView:
    """Verify one execution's records and return a consistent view. Raises ExecutionIntegrityError.

    The hash chain detects modified, removed or reordered events. It is not a signature: an actor
    able to rewrite the whole store could recompute it (see docs/governance-persistence.md)."""
    def fail(msg):
        raise ExecutionIntegrityError(f"execution {record.execution_id}: {msg}")

    if (record.proposal_id, record.content_hash, record.binding_digest, record.account_id) != \
            (stored.proposal_id, stored.content_hash, stored.binding_digest, stored.account_id):
        fail("record does not match the stored proposal")
    if lease is None or (lease.execution_id, lease.proposal_id) != (record.execution_id, record.proposal_id):
        fail("lease missing or for another execution")
    starts = [e for e in lifecycle if e.event_type == EventType.EXECUTION_STARTED
              and e.details.get("execution_id") == record.execution_id]
    if len(starts) != 1:
        fail("expected exactly one EXECUTION_STARTED lifecycle event")
    start = starts[0]
    if start.details.get("approval_id") != record.approval_id or start.actor != record.requested_by:
        fail("lifecycle start does not match the execution record")
    if start.details.get("person_id") != record.executor_identity.person_id:
        fail("executor identity does not match the lifecycle start")
    approvals = [e for e in lifecycle if e.event_type == EventType.APPROVED and e.seq < start.seq]
    if not approvals or approvals[-1].details.get("decision_id") != record.approval_id:
        fail("execution does not reference the approval that preceded it")
    if approvals[-1].details.get("approval_expires_at") != record.approval_expires_at.isoformat():
        fail("execution record's approval expiry does not match the approval")
    if approvals[-1].details.get("person_id") != record.approver_identity.person_id:
        fail("approver identity does not match the approval")
    approved = {r.resource_arn: {(c.key, c.new_value) for c in r.changes} for r in stored.load().records if r.changes}

    prev, last_token, results, summary, closing = record.record_hash(), 0, {}, None, None
    for expected_seq, e in enumerate(events, start=1):
        if e.seq != expected_seq:
            fail(f"event sequence gap at {expected_seq}")
        if (e.execution_id, e.proposal_id) != (record.execution_id, record.proposal_id):
            fail(f"event {e.seq} belongs to another execution")
        if e.prev_hash != prev or e.event_hash != e.compute_hash():
            fail(f"hash chain broken at event {e.seq}")
        if e.lease_token < last_token or e.lease_token > lease.token:
            fail(f"event {e.seq} has an invalid lease token")
        if summary is not None:
            fail("events recorded after finalization")
        if e.event_type == ExecutionEventType.RESOURCE_RESULT:
            if e.resource_arn not in record.intended_resources:
                fail(f"result for a resource outside the approved scope: {e.resource_arn}")
            results[e.resource_arn] = e.result
        if e.event_type == ExecutionEventType.REVALIDATED and e.resource_arn not in record.intended_resources:
            fail(f"revalidation of a resource outside the approved scope: {e.resource_arn}")
        if e.event_type == ExecutionEventType.WRITE_INTENT:
            if e.resource_arn not in record.intended_resources:
                fail(f"write intent for a resource outside the approved scope: {e.resource_arn}")
            if not {(c.key, c.new_value) for c in e.intent_changes} <= approved[e.resource_arn]:
                fail(f"write intent {e.seq} announces changes that were never approved")
            if e.write_deadline > record.approval_expires_at:
                fail(f"write intent {e.seq} extends past the approval expiry")
        if e.event_type in _CLOSING:
            summary, closing = e.summary, e.event_type
        prev, last_token = e.event_hash, e.lease_token

    in_flight = in_flight_writes(events)
    ends = [e for e in lifecycle if e.event_type in _EXECUTION_END and e.details.get("execution_id") == record.execution_id]
    if summary is None:
        if ends or lease.released:
            fail("lifecycle says finished but no FINALIZED or ABORTED event exists")
    else:
        if len(ends) != 1 or _EXECUTION_END[ends[0].event_type] != summary.outcome or not lease.released:
            fail("closing summary does not match the lifecycle outcome")
        aborted = closing == ExecutionEventType.ABORTED
        if not aborted and in_flight:
            fail(f"finalized with write intents that have no recorded outcome: {in_flight}")
        if summary != summarize(record.intended_resources, results, aborted=aborted, in_flight=in_flight):
            fail("closing summary does not match the recorded results")
    return ExecutionView(record=record, lease=lease, events=events, results=results, summary=summary)


def describe(events: list[LifecycleEvent]) -> list[dict]:
    """Who did what, when: a compact, ordered audit trail."""
    return [
        {"seq": e.seq, "at": e.at.isoformat(), "event": e.event_type.value,
         "transition": f"{e.from_state.value if e.from_state else '-'} -> {e.to_state.value}",
         "actor": f"{e.actor.actor_type.value}:{e.actor.id}", "reason": e.reason}
        for e in events
    ]
