"""Checkpoint 4.1: pure lifecycle state machine and event model."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cloudguard.governance.remediation import lifecycle
from cloudguard.governance.remediation.lifecycle import (
    EXECUTABLE_STATES,
    OPERATIONAL_EVENT_TYPES,
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransitionError,
    event_type_for,
    replay,
)
from cloudguard.governance.remediation.models import (
    Actor,
    ActorType,
    EventType,
    LifecycleEvent,
    ProposalState,
)

S = ProposalState
NOW = datetime(2026, 10, 14, 6, 30, tzinfo=UTC)
ACTOR = Actor(actor_type=ActorType.SYSTEM, id="test")
ALL_FROM = [None, *ProposalState]

LEGAL = {
    (None, S.PENDING), (S.PENDING, S.APPROVED), (S.PENDING, S.REJECTED), (S.PENDING, S.EXPIRED),
    (S.PENDING, S.STALE), (S.APPROVED, S.REVOKED), (S.APPROVED, S.EXPIRED), (S.APPROVED, S.STALE),
    (S.APPROVED, S.EXECUTING), (S.EXECUTING, S.SUCCEEDED), (S.EXECUTING, S.PARTIAL), (S.EXECUTING, S.FAILED),
    (S.EXECUTING, S.ABORTED),  # Batch 2 (M1): ABORT decision
}


@pytest.mark.parametrize("from_state", ALL_FROM)
@pytest.mark.parametrize("to_state", list(ProposalState))
def test_full_transition_matrix(from_state, to_state):
    if (from_state, to_state) in LEGAL:
        assert event_type_for(from_state, to_state) == TRANSITIONS[(from_state, to_state)]
    else:
        with pytest.raises(IllegalTransitionError):
            event_type_for(from_state, to_state)


def test_transition_table_is_exactly_the_legal_set():
    assert set(TRANSITIONS) == LEGAL


def test_terminal_states_never_transition():
    assert TERMINAL_STATES == {S.REJECTED, S.REVOKED, S.EXPIRED, S.STALE, S.SUCCEEDED, S.PARTIAL, S.FAILED,
                               S.ABORTED}
    for terminal in TERMINAL_STATES:
        assert not [t for (f, t) in TRANSITIONS if f == terminal]
        for target in ProposalState:
            with pytest.raises(IllegalTransitionError):
                event_type_for(terminal, target)


def test_no_path_returns_to_pending_or_an_executable_state():
    assert EXECUTABLE_STATES == {S.APPROVED}
    assert [f for (f, t) in TRANSITIONS if t == S.PENDING] == [None]
    assert [f for (f, t) in TRANSITIONS if t == S.APPROVED] == [S.PENDING]
    assert [f for (f, t) in TRANSITIONS if t == S.EXECUTING] == [S.APPROVED]


def test_only_creation_and_expiry_are_operational_in_checkpoint_4_1():
    assert OPERATIONAL_EVENT_TYPES == {EventType.PROPOSAL_CREATED, EventType.PROPOSAL_EXPIRED}


def event(seq, frm, to, **kw):
    return LifecycleEvent(proposal_id="proposal-00000000-0000-4000-8000-000000000000", seq=seq,
                          event_type=event_type_for(frm, to), from_state=frm, to_state=to, actor=ACTOR,
                          at=NOW, reason="r", content_hash="sha256:x", **kw)


def test_replay_returns_state_and_sequence():
    events = [event(1, None, S.PENDING), event(2, S.PENDING, S.APPROVED), event(3, S.APPROVED, S.EXECUTING),
              event(4, S.EXECUTING, S.PARTIAL)]
    assert replay(events) == (S.PARTIAL, 4)


@pytest.mark.parametrize("events", [
    [],
    [event(2, S.PENDING, S.EXPIRED)],                                   # doesn't start at 1
    [event(1, None, S.PENDING), event(3, S.PENDING, S.EXPIRED)],         # gap
    [event(1, None, S.PENDING), event(2, S.APPROVED, S.EXECUTING)],      # wrong previous state
    [event(1, None, S.PENDING), event(2, S.PENDING, S.EXPIRED), event(3, S.PENDING, S.APPROVED)],
])
def test_replay_rejects_invalid_histories(events):
    with pytest.raises(IllegalTransitionError):
        replay(events)


@pytest.mark.parametrize("kwargs", [
    dict(seq=1, event_type=EventType.PROPOSAL_EXPIRED, from_state=None, to_state=S.PENDING),   # wrong type
    dict(seq=2, event_type=EventType.PROPOSAL_CREATED, from_state=None, to_state=S.PENDING),   # creation not seq 1
    dict(seq=1, event_type=EventType.PROPOSAL_EXPIRED, from_state=S.PENDING, to_state=S.EXPIRED),  # seq1 with prior
    dict(seq=2, event_type=EventType.APPROVED, from_state=S.EXPIRED, to_state=S.APPROVED),    # illegal
])
def test_event_model_rejects_inconsistent_events(kwargs):
    with pytest.raises(ValidationError):
        LifecycleEvent(proposal_id="p", actor=ACTOR, at=NOW, reason="r", content_hash="h", **kwargs)


@pytest.mark.parametrize("at", [datetime(2026, 10, 14, 6, 30), datetime(2026, 10, 14, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))])
def test_event_timestamps_must_be_utc(at):
    with pytest.raises(ValidationError, match="UTC"):
        LifecycleEvent(proposal_id="p", seq=1, event_type=EventType.PROPOSAL_CREATED, from_state=None,
                       to_state=S.PENDING, actor=ACTOR, at=at, reason="r", content_hash="h")


def test_events_are_immutable():
    e = event(1, None, S.PENDING)
    with pytest.raises(ValidationError):
        e.to_state = S.APPROVED


def test_lifecycle_module_has_no_io():
    import inspect
    source = inspect.getsource(lifecycle)
    for forbidden in ("boto3", "open(", "datetime.now", "Path(", "requests"):
        assert forbidden not in source
