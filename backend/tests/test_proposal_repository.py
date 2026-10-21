"""Checkpoint 4.1: proposal persistence, idempotency, expiry, integrity and concurrency.

Every repository test runs against three backends: in-memory, on-disk and DynamoDB (moto).
"""

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError

from cloudguard.governance.__main__ import main
from cloudguard.governance.custom_tagging import CustomTaggingEngine
from cloudguard.governance.discovery import FixtureSource
from cloudguard.governance.policy import DEFAULT_POLICY_DOCUMENT, PolicyIntegrityError, parse_policy
from cloudguard.governance.policy_store import LocalPolicyStore, PolicyManager, PolicyNotFoundError
from cloudguard.governance.remediation import repository as repo_module
from cloudguard.governance.remediation.audit import describe, reconstruct_head
from cloudguard.governance.remediation.authz import BOOTSTRAP_ENV
from cloudguard.governance.remediation.models import (
    Actor,
    ActorType,
    EventType,
    LifecycleEvent,
    ProposalIntegrityError,
    ProposalState,
)
from cloudguard.governance.remediation.repository import (
    IDEMPOTENCY_RETENTION,
    IDEMPOTENCY_SCOPE,
    DuplicateProposalError,
    DynamoProposalRepository,
    IdempotencyConflictError,
    IdempotencyKeyTakenError,
    LocalProposalRepository,
    ProposalManager,
    ProposalNotApprovableError,
    ProposalNotFoundError,
    ProposalTooLargeError,
    TransitionConflictError,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "backend" / "fixtures" / "custom-tagging-demo.json"
NOW = datetime(2026, 10, 14, 6, 30, tzinfo=UTC)
REQUESTER = Actor(actor_type=ActorType.UNAUTHENTICATED, id="tester")
TABLE = "cloudguard-governance-test"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """CLI defaults (.cloudguard/...) are relative to the working directory: keep each test isolated."""
    monkeypatch.chdir(tmp_path)


class Clock:
    def __init__(self, now=NOW + timedelta(minutes=1)):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(scope="module")
def policies():
    pm = PolicyManager(LocalPolicyStore())
    pm.ensure_default()
    return pm


def build(policies, text="Tag all Lambda functions with Project=CloudGuard"):
    return CustomTaggingEngine(FixtureSource(FIXTURE), policies.get_active(), clock=lambda: NOW).prepare(text).proposal


@pytest.fixture(params=["memory", "disk", "dynamodb"])
def repo(request, tmp_path):
    if request.param == "memory":
        yield LocalProposalRepository()
    elif request.param == "disk":
        yield LocalProposalRepository(tmp_path / "proposals")
    else:
        with mock_aws():
            client = boto3.client("dynamodb", region_name="ap-south-1", aws_access_key_id="t", aws_secret_access_key="t")
            client.create_table(
                TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                      {"AttributeName": "SK", "AttributeType": "S"}],
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            )
            yield DynamoProposalRepository(client, TABLE)


def manager(repo, policies, clock=None):
    return ProposalManager(repo, clock=clock or Clock(), policy_manager=policies)


# ---------------------------------------------------------------- raw-record helpers (simulate tampering/crashes)

def meta_name(pid):
    return f"proposals/{pid}/META.json"


def read_raw_meta(repo, pid) -> dict:
    if isinstance(repo, DynamoProposalRepository):
        return repo.client.get_item(TableName=TABLE, Key={"PK": {"S": f"PROPOSAL#{pid}"}, "SK": {"S": "META"}})["Item"]
    return json.loads(repo.files.read(meta_name(pid)))


def tamper_meta(repo, pid, field, value):
    if isinstance(repo, DynamoProposalRepository):
        attr = {"policy": "policy_hash"}.get(field, field)
        repo.client.update_item(
            TableName=TABLE, Key={"PK": {"S": f"PROPOSAL#{pid}"}, "SK": {"S": "META"}},
            UpdateExpression="SET #a = :v", ExpressionAttributeNames={"#a": attr},
            ExpressionAttributeValues={":v": {"S": value if field != "policy" else "sha256:" + "0" * 64}},
        )
        return
    data = json.loads(repo.files.read(meta_name(pid)))
    if field == "policy":
        data["policy"]["content_hash"] = "sha256:" + "0" * 64
    else:
        data[field] = value
    repo.files.replace(meta_name(pid), json.dumps(data))


def write_raw_event(repo, event: LifecycleEvent):
    if isinstance(repo, DynamoProposalRepository):
        repo.client.put_item(TableName=TABLE, Item=DynamoProposalRepository._event_item(event))
    else:
        repo.files.create_exclusive(f"proposals/{event.proposal_id}/EVENT-{event.seq:010d}.json", event.model_dump_json())


def write_raw_head(repo, head):
    if isinstance(repo, DynamoProposalRepository):
        repo.client.put_item(TableName=TABLE, Item=DynamoProposalRepository._head_item(head))
    else:
        repo.files.replace(f"proposals/{head.proposal_id}/HEAD.json", head.model_dump_json())


# ================================================================ create / read

def test_save_and_retrieve_roundtrip(repo, policies):
    p = build(policies)
    m = manager(repo, policies)
    result = m.save(p, idempotency_key="key-roundtrip", requested_by=REQUESTER)
    assert result.created and result.head.state == ProposalState.PENDING and result.head.seq == 1
    view = m.get(p.proposal_id)
    assert view.proposal == p and view.head == result.head and view.created_by == REQUESTER
    assert not view.created_by.authenticated
    (created,) = m.history(p.proposal_id)
    assert created.event_type == EventType.PROPOSAL_CREATED and created.actor == REQUESTER
    assert created.details["requester_authenticated"] is False
    assert m.is_approvable_now(p.proposal_id)


@pytest.mark.parametrize("text", ["Tag ServiceWRK with Project=CloudGuard",            # NO_CHANGES
                                  "Add Environment=qa to all DynamoDB tables"])          # BLOCKED
def test_only_approvable_proposals_are_stored(repo, policies, text):
    p = build(policies, text)
    with pytest.raises(ProposalNotApprovableError):
        manager(repo, policies).save(p, idempotency_key="key-notapprovable", requested_by=REQUESTER)
    assert repo.get_stored(p.proposal_id) is None


def test_expired_proposals_are_not_stored(repo, policies):
    p = build(policies)
    with pytest.raises(ProposalNotApprovableError, match="expired"):
        manager(repo, policies, Clock(p.expires_at)).save(p, idempotency_key="key-expired01", requested_by=REQUESTER)


def test_unknown_policy_reference_is_rejected(repo, policies):
    other = PolicyManager(LocalPolicyStore())
    other.ensure_default()
    data = DEFAULT_POLICY_DOCUMENT.model_dump()
    data["defaults"].append({"key": "Team"})
    other.save(parse_policy(data), expected_version=1, created_by="t")
    p = CustomTaggingEngine(FixtureSource(FIXTURE), other.get_active(), clock=lambda: NOW).prepare(
        "Tag all Lambda functions with Project=CloudGuard").proposal  # references policy v2
    with pytest.raises(PolicyNotFoundError):
        manager(repo, policies).save(p, idempotency_key="key-badpolicy", requested_by=REQUESTER)


def test_missing_and_invalid_ids(repo, policies):
    m = manager(repo, policies)
    with pytest.raises(ProposalNotFoundError):
        m.get("proposal-00000000-0000-4000-8000-000000000000")
    for bad in ("../../etc/passwd", "proposal-1", "PROPOSAL#x"):
        with pytest.raises(ProposalNotFoundError):
            m.get(bad)


def test_proposal_too_large(repo, policies, monkeypatch):
    monkeypatch.setattr(repo_module, "MAX_PROPOSAL_BYTES", 1000)
    with pytest.raises(ProposalTooLargeError):
        manager(repo, policies).save(build(policies), idempotency_key="key-toolarge", requested_by=REQUESTER)


@pytest.mark.parametrize("key", ["short", "has space here", "x" * 129, "semi;colon-key"])
def test_invalid_idempotency_keys(repo, policies, key):
    with pytest.raises(ValidationError):
        manager(repo, policies).save(build(policies), idempotency_key=key, requested_by=REQUESTER)


# ================================================================ idempotency

def test_duplicate_request_returns_the_original_proposal(repo, policies):
    m = manager(repo, policies)
    first = build(policies)
    second = build(policies)  # same request, new proposal id
    assert first.proposal_id != second.proposal_id
    a = m.save(first, idempotency_key="key-duplicate", requested_by=REQUESTER)
    b = m.save(second, idempotency_key="key-duplicate", requested_by=REQUESTER)
    assert a.created and not b.created and b.proposal_id == first.proposal_id
    assert repo.get_stored(second.proposal_id) is None
    assert len(m.history(first.proposal_id)) == 1


def test_same_key_different_request_is_a_conflict(repo, policies):
    m = manager(repo, policies)
    m.save(build(policies), idempotency_key="key-conflict", requested_by=REQUESTER)
    with pytest.raises(IdempotencyConflictError):
        m.save(build(policies, "Tag all Lambda functions with Team=x"), idempotency_key="key-conflict",
               requested_by=REQUESTER)


def test_repository_rejects_second_use_of_a_key_at_storage_level(repo, policies):
    """Simulates two creates racing past the manager's pre-check."""
    m = manager(repo, policies)
    a, b = build(policies), build(policies)
    m.save(a, idempotency_key="key-raceraw", requested_by=REQUESTER)
    from cloudguard.governance.remediation.models import IdempotencyRecord, ProposalHead, StoredProposal

    stored = StoredProposal.from_proposal(b, created_by=REQUESTER, idempotency_key="key-raceraw", stored_at=NOW)
    head = ProposalHead(proposal_id=b.proposal_id, state=ProposalState.PENDING, seq=1, content_hash=b.content_hash,
                        proposal_expires_at=b.expires_at, updated_at=NOW)
    ev = LifecycleEvent(proposal_id=b.proposal_id, seq=1, event_type=EventType.PROPOSAL_CREATED, from_state=None,
                        to_state=ProposalState.PENDING, actor=REQUESTER, at=NOW, reason="r", content_hash=b.content_hash)
    rec = IdempotencyRecord(scope=IDEMPOTENCY_SCOPE, key="key-raceraw", request_fingerprint=stored.request_fingerprint,
                            proposal_id=b.proposal_id, created_at=NOW, ttl_epoch=1)
    with pytest.raises(IdempotencyKeyTakenError) as taken:
        repo.create(stored, head, ev, rec)
    assert taken.value.record.proposal_id == a.proposal_id
    # The manager resolves the same race into a replay of the first proposal.
    assert m._replay(taken.value.record, stored.request_fingerprint).proposal_id == a.proposal_id


def test_concurrent_creates_with_one_key_create_exactly_one_proposal(tmp_path, policies):
    repo = LocalProposalRepository(tmp_path / "proposals")
    m = manager(repo, policies)
    proposals = [build(policies) for _ in range(8)]
    results, errors = [], []

    def worker(p):
        try:
            results.append(m.save(p, idempotency_key="key-concurrent", requested_by=REQUESTER))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(p,)) for p in proposals]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sum(r.created for r in results) == 1 and len({r.proposal_id for r in results}) == 1
    assert len([n for n in repo.files.list("proposals") if n.endswith("/META.json")]) == 1


def test_concurrent_creates_with_distinct_keys_create_distinct_proposals(tmp_path, policies):
    repo = LocalProposalRepository(tmp_path / "proposals")
    m = manager(repo, policies)
    proposals = [build(policies) for _ in range(6)]
    threads = [threading.Thread(target=m.save, args=(p,), kwargs={"idempotency_key": f"key-distinct-{i}",
                                                                   "requested_by": REQUESTER})
               for i, p in enumerate(proposals)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([n for n in repo.files.list("proposals") if n.endswith("/META.json")]) == 6


# ================================================================ expiry

def test_lazy_expiry_happens_once_and_blocks_approval(repo, policies):
    clock = Clock()
    m = manager(repo, policies, clock)
    p = build(policies)
    m.save(p, idempotency_key="key-expiry", requested_by=REQUESTER)
    clock.now = p.expires_at - timedelta(seconds=1)
    assert m.head(p.proposal_id).state == ProposalState.PENDING and m.is_approvable_now(p.proposal_id)
    clock.now = p.expires_at
    assert m.head(p.proposal_id).state == ProposalState.EXPIRED
    assert not m.is_approvable_now(p.proposal_id)
    for _ in range(3):
        m.get(p.proposal_id)
        clock.now += timedelta(hours=1)
    events = m.history(p.proposal_id)
    assert [e.event_type for e in events] == [EventType.PROPOSAL_CREATED, EventType.PROPOSAL_EXPIRED]
    expiry = events[1]
    assert expiry.actor.actor_type == ActorType.SYSTEM and expiry.from_state == ProposalState.PENDING
    assert expiry.details["proposal_expires_at"] == p.expires_at.isoformat()


def test_concurrent_expiry_attempts_append_exactly_one_event(repo, policies):
    clock = Clock()
    m1, m2 = manager(repo, policies, clock), manager(repo, policies, clock)
    p = build(policies)
    m1.save(p, idempotency_key="key-concexp", requested_by=REQUESTER)
    clock.now = p.expires_at + timedelta(seconds=5)
    stale = repo.get_head(p.proposal_id)  # both "read" PENDING
    m1._expire(stale)
    with pytest.raises(TransitionConflictError):
        m2._expire(stale)
    assert m2.head(p.proposal_id).state == ProposalState.EXPIRED
    assert [e.event_type for e in m2.history(p.proposal_id)].count(EventType.PROPOSAL_EXPIRED) == 1


def test_threaded_expiry_appends_exactly_one_event(tmp_path, policies):
    clock = Clock()
    repo = LocalProposalRepository(tmp_path / "proposals")
    p = build(policies)
    manager(repo, policies, clock).save(p, idempotency_key="key-threadexp", requested_by=REQUESTER)
    clock.now = p.expires_at + timedelta(minutes=1)
    states = []
    threads = [threading.Thread(target=lambda: states.append(manager(repo, policies, clock).head(p.proposal_id).state))
               for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert states == [ProposalState.EXPIRED] * 10
    assert len(repo.list_events(p.proposal_id)) == 2


def test_clock_must_be_utc(repo, policies):
    m = ProposalManager(repo, clock=lambda: datetime(2026, 10, 14, 6, 31), policy_manager=policies)
    with pytest.raises(ValueError, match="UTC"):
        m.save(build(policies), idempotency_key="key-naiveclock", requested_by=REQUESTER)


# ================================================================ optimistic concurrency / append-only

def test_state_head_sequence_conflicts(repo, policies):
    m = manager(repo, policies)
    p = build(policies)
    m.save(p, idempotency_key="key-seqconflict", requested_by=REQUESTER)
    head = repo.get_head(p.proposal_id)
    stale_variants = [head.model_copy(update={"seq": 7}), head.model_copy(update={"state": ProposalState.APPROVED}),
                      head.model_copy(update={"content_hash": "sha256:" + "0" * 64})]
    for expected in stale_variants:
        ev = LifecycleEvent(proposal_id=p.proposal_id, seq=expected.seq + 1, event_type=EventType.PROPOSAL_EXPIRED,
                            from_state=ProposalState.PENDING, to_state=ProposalState.EXPIRED,
                            actor=REQUESTER, at=NOW, reason="r", content_hash=p.content_hash)
        new = head.model_copy(update={"state": ProposalState.EXPIRED, "seq": expected.seq + 1})
        with pytest.raises(TransitionConflictError):
            repo.append_transition(ev, expected, new)
    assert repo.get_head(p.proposal_id) == head and len(repo.list_events(p.proposal_id)) == 1


def test_conflicting_transitions_from_the_same_head(repo, policies):
    """Two different transitions race from one head: exactly one wins, the history stays linear."""
    m = manager(repo, policies)
    p = build(policies)
    m.save(p, idempotency_key="key-twowriters", requested_by=REQUESTER)
    head = repo.get_head(p.proposal_id)

    def attempt(to_state):
        ev = LifecycleEvent(proposal_id=p.proposal_id, seq=2, event_type={ProposalState.EXPIRED: EventType.PROPOSAL_EXPIRED,
                                                                          ProposalState.STALE: EventType.MARKED_STALE}[to_state],
                            from_state=ProposalState.PENDING, to_state=to_state, actor=REQUESTER, at=NOW,
                            reason="race", content_hash=p.content_hash)
        repo.append_transition(ev, head, head.model_copy(update={"state": to_state, "seq": 2}))

    attempt(ProposalState.STALE)
    with pytest.raises(TransitionConflictError):
        attempt(ProposalState.EXPIRED)
    assert [e.to_state for e in repo.list_events(p.proposal_id)] == [ProposalState.PENDING, ProposalState.STALE]
    assert repo.get_head(p.proposal_id).state == ProposalState.STALE


def test_meta_is_immutable(repo, policies):
    m = manager(repo, policies)
    p = build(policies)
    m.save(p, idempotency_key="key-immutable", requested_by=REQUESTER)
    before = read_raw_meta(repo, p.proposal_id)
    from cloudguard.governance.remediation.models import IdempotencyRecord, ProposalHead, StoredProposal

    other_actor = Actor(actor_type=ActorType.UNAUTHENTICATED, id="intruder")
    stored = StoredProposal.from_proposal(p, created_by=other_actor, idempotency_key="key-overwrite", stored_at=NOW)
    head = ProposalHead(proposal_id=p.proposal_id, state=ProposalState.PENDING, seq=1, content_hash=p.content_hash,
                        proposal_expires_at=p.expires_at, updated_at=NOW)
    ev = LifecycleEvent(proposal_id=p.proposal_id, seq=1, event_type=EventType.PROPOSAL_CREATED, from_state=None,
                        to_state=ProposalState.PENDING, actor=other_actor, at=NOW, reason="r", content_hash=p.content_hash)
    rec = IdempotencyRecord(scope=IDEMPOTENCY_SCOPE, key="key-overwrite", request_fingerprint="x",
                            proposal_id=p.proposal_id, created_at=NOW, ttl_epoch=1)
    with pytest.raises(DuplicateProposalError):
        repo.create(stored, head, ev, rec)
    assert read_raw_meta(repo, p.proposal_id) == before
    assert m.get(p.proposal_id).created_by == REQUESTER


def test_events_are_append_only(repo, policies):
    clock = Clock()
    m = manager(repo, policies, clock)
    p = build(policies)
    m.save(p, idempotency_key="key-appendonly", requested_by=REQUESTER)
    original = repo.list_events(p.proposal_id)[0]
    rewritten = original.model_copy(update={"reason": "rewritten"})
    head = repo.get_head(p.proposal_id)
    if isinstance(repo, LocalProposalRepository):
        with pytest.raises(FileExistsError):
            repo.files.create_exclusive(f"proposals/{p.proposal_id}/EVENT-0000000001.json", rewritten.model_dump_json())
    else:
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError):
            repo.client.put_item(TableName=TABLE, Item=DynamoProposalRepository._event_item(rewritten),
                                 ConditionExpression="attribute_not_exists(PK)")
    assert repo.list_events(p.proposal_id) == [original] and repo.get_head(p.proposal_id) == head
    for cls in (LocalProposalRepository, DynamoProposalRepository, ProposalManager):
        assert not [n for n in dir(cls) if any(w in n.lower() for w in ("delete", "update_event", "remove", "overwrite"))]


# ================================================================ history reconstruction / head repair

def test_history_reconstructs_the_head(repo, policies):
    clock = Clock()
    m = manager(repo, policies, clock)
    p = build(policies)
    m.save(p, idempotency_key="key-reconstruct", requested_by=REQUESTER)
    stored = repo.get_stored(p.proposal_id)
    assert reconstruct_head(stored, repo.list_events(p.proposal_id)) == repo.get_head(p.proposal_id)
    clock.now = p.expires_at
    m.get(p.proposal_id)
    assert reconstruct_head(stored, repo.list_events(p.proposal_id)) == repo.get_head(p.proposal_id)
    trail = describe(m.history(p.proposal_id))
    assert [t["transition"] for t in trail] == ["- -> PENDING", "PENDING -> EXPIRED"]
    assert trail[0]["actor"] == "UNAUTHENTICATED:tester" and trail[1]["actor"] == "SYSTEM:cloudguard-lifecycle"


def test_head_behind_events_is_repaired_from_history(repo, policies):
    """Crash between appending an event and updating the head: events win, the head is repaired."""
    clock = Clock()
    m = manager(repo, policies, clock)
    p = build(policies)
    m.save(p, idempotency_key="key-repair", requested_by=REQUESTER)
    write_raw_event(repo, LifecycleEvent(
        proposal_id=p.proposal_id, seq=2, event_type=EventType.MARKED_STALE, from_state=ProposalState.PENDING,
        to_state=ProposalState.STALE, actor=REQUESTER, at=clock.now, reason="crash-test", content_hash=p.content_hash))
    assert repo.get_head(p.proposal_id).seq == 1
    assert m.head(p.proposal_id).state == ProposalState.STALE
    assert repo.get_head(p.proposal_id).seq == 2  # materialised view repaired


@pytest.mark.parametrize("change", [{"seq": 5}, {"state": ProposalState.APPROVED}])
def test_head_contradicting_history_is_an_integrity_error(repo, policies, change):
    m = manager(repo, policies)
    p = build(policies)
    m.save(p, idempotency_key="key-badhead", requested_by=REQUESTER)
    write_raw_head(repo, repo.get_head(p.proposal_id).model_copy(update=change))
    with pytest.raises(ProposalIntegrityError):
        m.get(p.proposal_id)


# ================================================================ tampering / corruption

@pytest.mark.parametrize(("field", "value"), [
    ("content_hash", "sha256:" + "f" * 64),
    ("binding_digest", "sha256:" + "f" * 64),
    ("request_fingerprint", "sha256:" + "f" * 64),
    ("schema_version", "cloudguard.tagging-proposal/v0"),
    ("policy", None),
])
def test_tampered_metadata_is_rejected(repo, policies, field, value):
    m = manager(repo, policies)
    p = build(policies)
    m.save(p, idempotency_key="key-tampermeta", requested_by=REQUESTER)
    tamper_meta(repo, p.proposal_id, field, value)
    with pytest.raises((ProposalIntegrityError, PolicyIntegrityError, PolicyNotFoundError)):
        m.get(p.proposal_id)


def test_tampered_proposal_body_is_rejected(repo, policies):
    m = manager(repo, policies)
    p = build(policies)
    m.save(p, idempotency_key="key-tamperbody", requested_by=REQUESTER)
    body = json.loads(p.model_dump_json())
    idx = next(i for i, r in enumerate(body["records"]) if r["changes"])
    body["records"][idx]["changes"][0]["new_value"] = "Evil"
    tamper_meta(repo, p.proposal_id, "proposal_json", json.dumps(body))
    with pytest.raises(ProposalIntegrityError, match="failed validation"):
        m.get(p.proposal_id)


def test_corrupt_local_records_are_detected(tmp_path, policies):
    repo = LocalProposalRepository(tmp_path / "proposals")
    m = manager(repo, policies)
    p = build(policies)
    m.save(p, idempotency_key="key-corrupt01", requested_by=REQUESTER)
    repo.files.replace(f"proposals/{p.proposal_id}/HEAD.json", "{not json")  # corrupt the stored record
    with pytest.raises(ProposalIntegrityError, match="corrupt"):
        m.get(p.proposal_id)


def test_disk_store_is_one_sqlite_database_with_logical_record_names(tmp_path, policies):
    """M4: records keep their logical names and JSON bodies inside one SQLite database; there are no
    per-record files, temp files or lock files on disk."""
    clock = Clock()
    repo = LocalProposalRepository(tmp_path / "proposals")
    m = manager(repo, policies, clock)
    p = build(policies)
    m.save(p, idempotency_key="key-cleanfiles", requested_by=REQUESTER)
    clock.now = p.expires_at
    m.get(p.proposal_id)
    names = {f.name for f in (tmp_path / "proposals").rglob("*") if f.is_file()}
    assert names <= {"proposals.sqlite3", "proposals.sqlite3-wal", "proposals.sqlite3-shm"}
    assert sorted(n.rsplit("/", 1)[-1] for n in repo.files.list(f"proposals/{p.proposal_id}")) == \
        ["EVENT-0000000001.json", "EVENT-0000000002.json", "HEAD.json", "META.json"]


def test_local_repository_survives_reload(tmp_path, policies):
    p = build(policies)
    manager(LocalProposalRepository(tmp_path / "s"), policies).save(p, idempotency_key="key-reload01", requested_by=REQUESTER)
    view = manager(LocalProposalRepository(tmp_path / "s"), policies).get(p.proposal_id)
    assert view.proposal == p and view.head.state == ProposalState.PENDING


# ================================================================ TTL separation

def test_ttl_only_on_temporary_records_in_dynamodb(policies):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-south-1", aws_access_key_id="t", aws_secret_access_key="t")
        client.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                                  {"AttributeName": "SK", "AttributeType": "S"}],
                            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                       {"AttributeName": "SK", "KeyType": "RANGE"}])
        clock = Clock()
        m = manager(DynamoProposalRepository(client, TABLE), policies, clock)
        p = build(policies)
        m.save(p, idempotency_key="key-ttlcheck", requested_by=REQUESTER)
        clock.now = p.expires_at
        m.get(p.proposal_id)
        items = client.scan(TableName=TABLE)["Items"]
    by_kind = {}
    for item in items:
        by_kind.setdefault(item["SK"]["S"].split("#")[0], []).append(item)
    assert set(by_kind) == {"META", "HEAD", "EVENT"}  # + IDEMP's SK is META too; separate by PK below
    for item in items:
        durable = item["PK"]["S"].startswith("PROPOSAL#")
        assert "expires_at" not in item  # the old TTL attribute name never appears
        if durable:
            assert "ttl_epoch" not in item, item["SK"]["S"]
        else:
            assert item["PK"]["S"].startswith("IDEMP#") and "ttl_epoch" in item
            assert int(item["ttl_epoch"]["N"]) >= int((p.expires_at + IDEMPOTENCY_RETENTION - timedelta(hours=1)).timestamp())
    meta = next(i for i in items if i["SK"]["S"] == "META" and i["PK"]["S"].startswith("PROPOSAL#"))
    assert meta["proposal_expires_at"]["S"] == p.expires_at.isoformat()


def test_idempotency_retention_outlives_any_proposal():
    from cloudguard.governance.custom_tagging.models import MAX_PROPOSAL_TTL
    assert IDEMPOTENCY_RETENTION > MAX_PROPOSAL_TTL * 24


def test_governance_table_has_no_ttl():
    """Option D (H2 follow-up): TTL is disabled on the governance table, because writers can set any
    attribute; ttl_epoch on IDEMP#/NONCE# items is only an inert retention hint."""
    template = (ROOT / "infra" / "cloudguard-core.yaml").read_text(encoding="utf-8")
    assert "TimeToLiveSpecification" not in template
    assert "must stay disabled" in template and "reopens" in template
    # H2: proposal-store writes moved to the participant policy - UpdateItem yes, DeleteItem never.
    participant = template[template.index("GovernanceParticipantPolicy:"):template.index("GovernanceAdminPolicy:")]
    assert "- dynamodb:UpdateItem" in participant and "dynamodb:DeleteItem" not in participant


# ================================================================ CLI

AUTHZ_FIXTURE = ROOT / "backend" / "fixtures" / "authz-demo.json"


def cli_store(tmp_path):
    store = ["--proposal-store", str(tmp_path / "p"), "--policy-store", str(tmp_path / "pol"),
             "--authz-store", str(tmp_path / "az")]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(BOOTSTRAP_ENV, "dev:admin")
        assert main(["authz", "save", str(AUTHZ_FIXTURE), "--expected-version", "0", "--dev-identity", "admin",
                     "--authz-store", str(tmp_path / "az")]) == 0
    return store


def test_cli_proposal_save_show_history(tmp_path, capsys):
    store = cli_store(tmp_path)
    capsys.readouterr()
    assert main(["proposal", "save", "Tag all Lambda functions with Project=CloudGuard", "--dev-identity", "alice",
                 "--idempotency-key", "cli-key-0001", "--fixture", str(FIXTURE), *store]) == 0
    saved = json.loads(capsys.readouterr().out)
    pid = saved["proposal_id"]
    assert saved["created"] is True and saved["head"]["state"] == "PENDING"
    assert main(["proposal", "save", "Tag all Lambda functions with Project=CloudGuard", "--dev-identity", "alice",
                 "--idempotency-key", "cli-key-0001", "--fixture", str(FIXTURE), *store]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["created"] is False and replay["proposal_id"] == pid
    read_store = store[:4]
    assert main(["proposal", "show", pid, *read_store]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["head"]["state"] == "PENDING"
    assert shown["created_by"] == {"actor_type": "DEVELOPMENT", "id": "dev:alice"}
    assert main(["proposal", "history", pid, *read_store]) == 0
    assert [e["event_type"] for e in json.loads(capsys.readouterr().out)] == ["PROPOSAL_CREATED"]


def test_cli_rejects_non_approvable_and_conflicting_saves(tmp_path, capsys):
    store = [*cli_store(tmp_path), "--fixture", str(FIXTURE), "--dev-identity", "alice"]
    capsys.readouterr()
    assert main(["proposal", "save", "Tag ServiceWRK with Project=CloudGuard", *store]) == 1
    assert "only approvable proposals are stored" in capsys.readouterr().err
    assert main(["proposal", "save", "Tag ServiceWRK with A=B", "--idempotency-key", "cli-key-0002", *store]) == 0
    assert main(["proposal", "save", "Tag ServiceWRK with A=C", "--idempotency-key", "cli-key-0002", *store]) == 1
    assert "different request" in capsys.readouterr().err


def test_cli_save_requires_an_identity_provider_identity(tmp_path, capsys):
    store = [*cli_store(tmp_path), "--fixture", str(FIXTURE)]
    with pytest.raises(SystemExit) as exc:
        main(["proposal", "save", "Tag ServiceWRK with A=B", *store])  # no --dev-identity
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        main(["proposal", "save", "Tag ServiceWRK with A=B", "--requested-by", "someone", *store])  # label flag is gone


def test_cli_exposes_no_execution_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["proposal", "execute", "proposal-00000000-0000-4000-8000-000000000000"])
    assert exc.value.code == 2
    # Decisions live only in DecisionService (identity-bound); the storage manager has none.
    assert not [n for n in dir(ProposalManager) if n.lstrip("_").startswith(("approve", "reject", "revoke", "execut"))]


@pytest.mark.parametrize("name", ["../escape.json", "proposals/../../x", "/abs/path", "a//b", "proposals/./x",
                                  "C:/windows/x", r"proposals\..\x", "p/x y"])
def test_disk_store_rejects_unsafe_paths(tmp_path, name):
    files = LocalProposalRepository(tmp_path / "s").files
    with pytest.raises(ValueError, match="unsafe repository path"):
        files.read(name)
