"""Proposal lifecycle state machine. Pure functions only: no I/O, no clock.

    (create) ─► PENDING ─approve─► APPROVED ─start─► EXECUTING ─► SUCCEEDED | PARTIAL | FAILED | ABORTED
                  │ │ │             │ │ │
          reject  │ │ │ stale  revoke │ │ │ stale
                  ▼ │ ▼             ▼ │ ▼
             REJECTED│ STALE     REVOKED│ STALE
                  expire               expire
                     ▼                    ▼
                  EXPIRED              EXPIRED

Terminal states never transition again; in particular nothing ever returns to an executable
state (APPROVED) or to PENDING. Checkpoint 4.1 exposes only creation and expiry as operations.
"""

from __future__ import annotations

from cloudguard.governance.remediation.models import EventType, ProposalState

S = ProposalState

TRANSITIONS: dict[tuple[ProposalState | None, ProposalState], EventType] = {
    (None, S.PENDING): EventType.PROPOSAL_CREATED,
    (S.PENDING, S.APPROVED): EventType.APPROVED,
    (S.PENDING, S.REJECTED): EventType.REJECTED,
    (S.PENDING, S.EXPIRED): EventType.PROPOSAL_EXPIRED,
    (S.PENDING, S.STALE): EventType.MARKED_STALE,
    (S.APPROVED, S.REVOKED): EventType.REVOKED,
    (S.APPROVED, S.EXPIRED): EventType.PROPOSAL_EXPIRED,
    (S.APPROVED, S.STALE): EventType.MARKED_STALE,
    (S.APPROVED, S.EXECUTING): EventType.EXECUTION_STARTED,
    (S.EXECUTING, S.SUCCEEDED): EventType.EXECUTION_SUCCEEDED,
    (S.EXECUTING, S.PARTIAL): EventType.EXECUTION_PARTIAL,
    (S.EXECUTING, S.FAILED): EventType.EXECUTION_FAILED,
    (S.EXECUTING, S.ABORTED): EventType.EXECUTION_ABORTED,  # Batch 2 (M1): ABORT decision
}

TERMINAL_STATES = frozenset({S.REJECTED, S.REVOKED, S.EXPIRED, S.STALE, S.SUCCEEDED, S.PARTIAL, S.FAILED, S.ABORTED})
EXECUTABLE_STATES = frozenset({S.APPROVED})  # the only state from which execution may start
EXPIRABLE_STATES = frozenset({S.PENDING, S.APPROVED})

# Transitions that have a callable operation in Checkpoint 4.1.
OPERATIONAL_EVENT_TYPES = frozenset({EventType.PROPOSAL_CREATED, EventType.PROPOSAL_EXPIRED})


class IllegalTransitionError(ValueError):
    pass


def event_type_for(from_state: ProposalState | None, to_state: ProposalState) -> EventType:
    """Event type for a legal transition; raises IllegalTransitionError otherwise."""
    try:
        return TRANSITIONS[(from_state, to_state)]
    except KeyError:
        raise IllegalTransitionError(f"illegal transition {from_state} -> {to_state}") from None


def is_terminal(state: ProposalState) -> bool:
    return state in TERMINAL_STATES


def replay(events: list) -> tuple[ProposalState, int]:
    """Validate an ordered event stream and return (current state, last seq).

    Sequence numbers must be contiguous from 1, each event must start from the previous event's
    state, and every step must be a legal transition."""
    state: ProposalState | None = None
    for expected_seq, event in enumerate(events, start=1):
        if event.seq != expected_seq:
            raise IllegalTransitionError(f"event sequence gap: expected {expected_seq}, found {event.seq}")
        if event.from_state != state:
            raise IllegalTransitionError(f"event {event.seq} starts from {event.from_state}, but state was {state}")
        event_type_for(event.from_state, event.to_state)
        state = event.to_state
    if state is None:
        raise IllegalTransitionError("no events")
    return state, len(events)
