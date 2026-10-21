"""M4: proposal-store crash consistency, consistent reads and DynamoDB cancellation mapping.

Backends:
- "memory": the in-memory store (transactions with rollback);
- "sqlite": the local on-disk store (one SQLite database, WAL, synchronous=FULL);
- "dynamodb": moto - moto is NOT AWS, so it shows our logic, not DynamoDB's real behaviour;
- FakeDynamo (cancellation-mapping tests): a hand-written mock that raises botocore ClientErrors
  shaped like DynamoDB's TransactionCanceledException. Also NOT AWS.
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from cloudguard.governance.__main__ import main
from cloudguard.governance.custom_tagging import CustomTaggingEngine
from cloudguard.governance.remediation.approval import new_nonce
from cloudguard.governance.remediation.authz import BOOTSTRAP_ENV
from cloudguard.governance.remediation.execution_models import ExecutionEvent
from cloudguard.governance.remediation.models import (
    DecisionRecord,
    IdempotencyRecord,
    LifecycleEvent,
    NonceRecord,
    ProposalHead,
    ProposalIntegrityError,
    ProposalState,
)
from cloudguard.governance.remediation.repository import (
    ApprovalNoLongerValidError,
    ConsistentReadError,
    DuplicateProposalError,
    DynamoProposalRepository,
    IdempotencyKeyTakenError,
    LeaseConflictError,
    LocalProposalRepository,
    NonceReplayError,
    TransientStoreError,
    TransitionConflictError,
    read_proposal_state,
)
from cloudguard.governance.remediation.writers import SimulatedCrash
from test_execution import LAMBDA, T0, World, _crash_at, _isolated_cwd, repo  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
TARGET = LAMBDA + "shared-name"


@pytest.fixture(params=["memory", "sqlite"])
def local_repo(request, tmp_path):
    r = LocalProposalRepository() if request.param == "memory" else LocalProposalRepository(tmp_path / "store")
    yield r
    r.close()


def dump(repo) -> dict[str, str]:
    """Every stored record (name -> body)."""
    if hasattr(repo.files, "data"):
        return dict(repo.files.data)
    return {n: repo.files.read(n) for prefix in ("proposals", "idempotency", "nonces") for n in repo.files.list(prefix)}


def second_proposal(w):
    return CustomTaggingEngine(w.account, w.policies.get_active(), clock=lambda: T0).prepare(
        "Add Owner=data-team to all DynamoDB tables").proposal


def crash_inside(repo, method: str, position: int, after: bool, captured: dict):
    """Crash inside the FIRST call of `repo.<method>`: before (or right after) its `position`-th write.
    'after the last write' is 'just before COMMIT'. Records the store contents at method entry."""
    real = getattr(repo, method)

    def wrapped(*a, **kw):
        setattr(repo, method, real)  # only the first call crashes
        captured["before"] = dump(repo)
        files = repo.files
        oc, orp = files.create_exclusive, files.replace
        n = [0]

        def gate(fn):
            def g(name, *x):
                n[0] += 1
                if n[0] == position and not after:
                    raise SimulatedCrash(f"{method}: before write {position}")
                result = fn(name, *x)
                if n[0] == position and after:
                    raise SimulatedCrash(f"{method}: after write {position}, before commit")
                return result
            return g
        files.create_exclusive, files.replace = gate(oc), gate(orp)
        try:
            return real(*a, **kw)
        finally:
            files.create_exclusive, files.replace = oc, orp
    setattr(repo, method, wrapped)


def positions(n):
    return [(k, False) for k in range(1, n + 1)] + [(n, True)]


# ================================================================ all-or-nothing on crash (memory + SQLite)

@pytest.mark.parametrize(("k", "after"), positions(4))
def test_create_is_all_or_nothing(local_repo, k, after):
    w = World(local_repo)
    p = second_proposal(w)
    captured = {}
    crash_inside(local_repo, "create", k, after, captured)
    with pytest.raises(SimulatedCrash):
        w.svc("alice").create_proposal(p, idempotency_key="key-crash-create")
    assert dump(local_repo) == captured["before"]
    retry = w.svc("alice").create_proposal(second_proposal(w), idempotency_key="key-crash-create")  # CLI: new id
    assert retry.created
    metas = [n for n in local_repo.files.list("proposals") if n.endswith("/META.json")]
    assert len(metas) == 2  # World's own proposal + exactly one for this request


@pytest.mark.parametrize(("k", "after"), positions(5))
def test_decision_is_all_or_nothing_and_the_nonce_is_spent_only_on_commit(local_repo, k, after):
    w = World(local_repo)
    p = second_proposal(w)
    w.svc("alice").create_proposal(p, idempotency_key="key-crash-c2")
    nonce = new_nonce()
    captured = {}
    crash_inside(local_repo, "record_decision", k, after, captured)
    with pytest.raises(SimulatedCrash):
        w.svc("bob").approve(p.proposal_id, content_hash=p.content_hash, nonce=nonce, idempotency_key="key-crash-appr")
    assert dump(local_repo) == captured["before"]
    assert local_repo.get_nonce(nonce) is None and w.proposals.head(p.proposal_id).state == ProposalState.PENDING
    d = w.svc("bob").approve(p.proposal_id, content_hash=p.content_hash, nonce=nonce, idempotency_key="key-crash-appr")
    assert d.created and local_repo.get_nonce(nonce) is not None  # spent exactly when it committed


@pytest.mark.parametrize(("k", "after"), positions(2))
def test_lazy_expiry_is_all_or_nothing(local_repo, k, after):
    w = World(local_repo)
    w.clock.now += timedelta(hours=2)
    captured = {}
    crash_inside(local_repo, "append_transition", k, after, captured)
    with pytest.raises(SimulatedCrash):
        w.proposals.get(w.proposal.proposal_id)
    assert dump(local_repo) == captured["before"]
    assert w.proposals.head(w.proposal.proposal_id).state == ProposalState.EXPIRED


@pytest.mark.parametrize(("method", "n"), [("start_execution", 5), ("append_execution_event", 2), ("finish_execution", 4)])
@pytest.mark.parametrize("where", ["first", "middle", "before-commit"])
def test_execution_writes_are_all_or_nothing_and_recoverable(local_repo, method, n, where):
    k, after = {"first": (1, False), "middle": (max(2, n // 2), False), "before-commit": (n, True)}[where]
    w = World(local_repo)
    captured = {}
    crash_inside(local_repo, method, k, after, captured)
    with pytest.raises(SimulatedCrash):
        w.execute()
    assert dump(local_repo) == captured["before"]
    w.clock.now += timedelta(minutes=3)
    view = w.execute(name="erin", key="key-exec-recover")
    assert view.summary.outcome == ProposalState.SUCCEEDED
    assert all(v.summary is not None for v in w.executor().history(w.proposal.proposal_id))  # history verifies


@pytest.mark.parametrize(("k", "after"), positions(7))
def test_abort_is_all_or_nothing(local_repo, k, after):
    w = World(local_repo)
    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", TARGET))
    eid = w.proposals.head(w.proposal.proposal_id).execution_id
    captured = {}
    crash_inside(local_repo, "record_abort", k, after, captured)

    def abort():
        return w.svc("bob").abort(w.proposal.proposal_id, content_hash=w.proposal.content_hash, execution_id=eid,
                                  nonce=new_nonce(), idempotency_key="key-abort-crash", reason="stop")
    with pytest.raises(SimulatedCrash):
        abort()
    assert dump(local_repo) == captured["before"]
    assert w.proposals.head(w.proposal.proposal_id).state == ProposalState.EXECUTING
    assert abort().created
    assert w.executor().load(w.proposal.proposal_id, eid).summary.outcome == ProposalState.ABORTED


# ================================================================ retries after a committed write whose response was lost

def lose_response(repo, method):
    """The write commits, then the caller crashes before seeing the result."""
    real = getattr(repo, method)

    def wrapped(*a, **kw):
        setattr(repo, method, real)
        real(*a, **kw)
        raise SimulatedCrash(f"{method}: committed, response lost")
    setattr(repo, method, wrapped)


def test_a_committed_create_is_never_duplicated_by_a_retry(repo):
    w = World(repo)
    lose_response(repo, "create")
    with pytest.raises(SimulatedCrash):
        w.svc("alice").create_proposal(second_proposal(w), idempotency_key="key-lost-create")
    retry = w.svc("alice").create_proposal(second_proposal(w), idempotency_key="key-lost-create")  # new id, same key
    assert retry.created is False
    assert repo.get_idempotency("tagging-proposal-create", "key-lost-create").proposal_id == retry.proposal_id


def test_decision_and_abort_retries_replay_the_committed_result(repo):
    w = World(repo)
    p = second_proposal(w)
    w.svc("alice").create_proposal(p, idempotency_key="key-lost-c2")
    lose_response(repo, "record_decision")
    with pytest.raises(SimulatedCrash):
        w.svc("bob").approve(p.proposal_id, content_hash=p.content_hash, nonce=new_nonce(), idempotency_key="key-lost-appr")
    replay = w.svc("bob").approve(p.proposal_id, content_hash=p.content_hash, nonce=new_nonce(),
                                  idempotency_key="key-lost-appr")
    assert replay.created is False and replay.head.state == ProposalState.APPROVED

    with pytest.raises(SimulatedCrash):
        w.execute(hooks=_crash_at("before_write", TARGET))
    eid = w.proposals.head(w.proposal.proposal_id).execution_id
    lose_response(repo, "record_abort")

    def abort():
        return w.svc("bob").abort(w.proposal.proposal_id, content_hash=w.proposal.content_hash, execution_id=eid,
                                  nonce=new_nonce(), idempotency_key="key-lost-abort", reason="stop")
    with pytest.raises(SimulatedCrash):
        abort()
    replayed = abort()
    assert replayed.created is False and replayed.head.state == ProposalState.ABORTED


def test_a_spent_nonce_cannot_be_reused(repo):
    w = World(repo)
    p = second_proposal(w)
    w.svc("alice").create_proposal(p, idempotency_key="key-nonce-c2")
    nonce = w.approval.nonce
    with pytest.raises(NonceReplayError):
        w.svc("bob").approve(p.proposal_id, content_hash=p.content_hash, nonce=nonce, idempotency_key="key-nonce-new")


# ================================================================ process termination and multiple processes (SQLite)

KILL_CHILD = r"""
import json, os, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1]]
from cloudguard.governance.remediation.models import (DecisionRecord, IdempotencyRecord, LifecycleEvent,
                                                      NonceRecord, ProposalHead)
from cloudguard.governance.remediation.repository import LocalProposalRepository
args = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
repo = LocalProposalRepository(Path(sys.argv[2]))
real = repo.files.create_exclusive
n = [0]
def gate(name, text):
    real(name, text)
    n[0] += 1
    if n[0] == 2:  # DECISION and EVENT written; the transaction is not committed
        os._exit(9)  # hard kill: no Python cleanup, no finally blocks
repo.files.create_exclusive = gate
repo.record_decision(LifecycleEvent.model_validate(args["event"]), ProposalHead.model_validate(args["expected"]),
                     ProposalHead.model_validate(args["new_head"]), DecisionRecord.model_validate(args["decision"]),
                     NonceRecord.model_validate(args["nonce"]), IdempotencyRecord.model_validate(args["idem"]))
"""


def test_a_process_killed_mid_transaction_leaves_no_partial_state_and_no_stale_lock(tmp_path):
    store = tmp_path / "store"
    repo = LocalProposalRepository(store)
    w = World(repo)
    p = second_proposal(w)
    w.svc("alice").create_proposal(p, idempotency_key="key-kill-c2")
    captured = {}
    real = repo.record_decision

    def capture(event, expected, new_head, decision, nonce, idem):
        captured.update(event=event, expected=expected, new_head=new_head, decision=decision, nonce=nonce, idem=idem)
        raise SimulatedCrash("captured; nothing written")
    repo.record_decision = capture
    with pytest.raises(SimulatedCrash):
        w.svc("bob").approve(p.proposal_id, content_hash=p.content_hash, nonce=new_nonce(), idempotency_key="key-kill-appr")
    repo.record_decision = real
    args_file = tmp_path / "args.json"
    args_file.write_text(json.dumps({k: v.model_dump(mode="json") for k, v in captured.items()}), encoding="utf-8")

    child = subprocess.run([sys.executable, "-c", KILL_CHILD, str(BACKEND), str(store), str(args_file)],
                           capture_output=True, text=True, timeout=120)
    assert child.returncode == 9, child.stderr

    assert not [f for f in store.rglob("*") if f.suffix == ".lock" or f.name.startswith(".tmp-")]
    assert w.proposals.head(p.proposal_id).state == ProposalState.PENDING  # nothing of the killed write survived
    assert not repo.list_decisions(p.proposal_id) and repo.get_nonce(captured["nonce"].nonce) is None
    started = time.monotonic()
    d = w.svc("bob").approve(p.proposal_id, content_hash=p.content_hash, nonce=new_nonce(), idempotency_key="key-kill-ok")
    assert d.created and time.monotonic() - started < 10  # no stale lock to wait for


def test_concurrent_decisions_from_separate_processes_commit_exactly_one(tmp_path, capsys, monkeypatch):
    s = ["--proposal-store", str(tmp_path / "p"), "--policy-store", str(tmp_path / "pol")]
    az = ["--authz-store", str(tmp_path / "az")]
    monkeypatch.setenv(BOOTSTRAP_ENV, "dev:admin")
    authz = ROOT / "backend" / "fixtures" / "authz-demo.json"
    assert main(["authz", "save", str(authz), "--expected-version", "0", "--dev-identity", "admin", *az]) == 0
    assert main(["proposal", "save", "Tag all Lambda functions with Project=CloudGuard", "--dev-identity", "alice",
                 "--fixture-account", str(tmp_path / "acct.json"), *s, *az]) == 0
    capsys.readouterr()
    assert main(["proposal", "save", "Tag all Lambda functions with Project=CloudGuard", "--dev-identity", "alice",
                 "--idempotency-key", "cli-mp-0001", "--fixture-account", str(tmp_path / "acct.json"), *s, *az]) == 0
    pid = json.loads(capsys.readouterr().out)["proposal_id"]
    assert main(["proposal", "show", pid, *s]) == 0
    h = json.loads(capsys.readouterr().out)["proposal"]["content_hash"]

    code = ("import sys; sys.path[:0] = [sys.argv[1]]; from cloudguard.governance.__main__ import main; "
            "raise SystemExit(main(sys.argv[2:]))")
    commands = [["approve", "--dev-identity", "bob"], ["approve", "--dev-identity", "carol"],
                ["reject", "--dev-identity", "alice", "--reason", "no"], ["reject", "--dev-identity", "carol", "--reason", "no"]]
    procs = [subprocess.Popen([sys.executable, "-c", code, str(BACKEND), "proposal", c[0], pid, "--content-hash", h,
                               *c[1:], *s, *az], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for c in commands]
    results = [(p.wait(timeout=180), p.stderr.read()) for p in procs]
    assert sorted(code for code, _ in results).count(0) == 1, results
    assert all(code in (0, 1, 3) for code, _ in results), results
    assert main(["proposal", "history", pid, *s]) == 0  # verifies events and decisions
    history = json.loads(capsys.readouterr().out)
    assert len(history) == 2 and history[1]["event_type"] in ("APPROVED", "REJECTED")


# ================================================================ consistent reads during concurrent writes

def test_reads_stay_consistent_while_another_thread_commits(repo):
    """The writer commits between the reader's event read and its later reads. Memory: the writer
    waits for the reader's snapshot. SQLite: the reader keeps its snapshot. DynamoDB (moto): the
    reader sees the head move and retries. Never an integrity error."""
    w = World(repo)
    p = second_proposal(w)
    w.svc("alice").create_proposal(p, idempotency_key="key-race-c2")
    reader = threading.get_ident()
    real = repo.list_events
    writer_result = {}

    def write():
        writer_result["d"] = w.svc("bob").approve(p.proposal_id, content_hash=p.content_hash, nonce=new_nonce(),
                                                  idempotency_key="key-race-appr")

    def list_events(pid):
        result = real(pid)
        if pid == p.proposal_id and threading.get_ident() == reader and "t" not in writer_result:
            t = threading.Thread(target=write)
            writer_result["t"] = t
            t.start()
            t.join(timeout=0.5 if isinstance(repo, LocalProposalRepository) and hasattr(repo.files, "data") else 30)
        return result
    repo.list_events = list_events
    view = w.proposals.get(p.proposal_id)
    # Local stores read one snapshot taken before the commit; DynamoDB saw the head move and re-read.
    expected = ProposalState.APPROVED if isinstance(repo, DynamoProposalRepository) else ProposalState.PENDING
    assert view.head.state == expected
    writer_result["t"].join(timeout=30)
    repo.list_events = real
    assert writer_result["d"].created and w.proposals.head(p.proposal_id).state == ProposalState.APPROVED


def test_reads_that_never_settle_raise_a_retryable_error_not_tampering():
    class MovingHead:
        def __init__(self):
            self.seq = 0

        def read_snapshot(self):
            import contextlib
            return contextlib.nullcontext()

        def get_head(self, _pid):
            self.seq += 1
            return self.seq  # changes between every read

        def get_stored(self, _pid):
            return None

        def list_events(self, _pid):
            return []

        def list_decisions(self, _pid):
            return []
    with pytest.raises(ConsistentReadError, match="kept changing") as exc:
        read_proposal_state(MovingHead(), "proposal-x", attempts=3)
    assert isinstance(exc.value, TransitionConflictError) and not isinstance(exc.value, ProposalIntegrityError)


def test_tampered_records_are_still_detected_on_the_sqlite_store(tmp_path):
    repo = LocalProposalRepository(tmp_path / "store")
    w = World(repo)
    pid = w.proposal.proposal_id
    name = f"proposals/{pid}/DECISION-{w.approval.decision_id}.json"
    data = json.loads(repo.files.read(name))
    data["identity"]["person_id"] = "mallory"
    repo.files.replace(name, json.dumps(data))
    with pytest.raises(ProposalIntegrityError):
        w.proposals.get(pid)
    repo.close()


# ================================================================ DynamoDB cancellation mapping (mock client, NOT AWS)

class FakeDynamo:
    """Raises TransactionCanceledException shaped as botocore parses it: CancellationReasons at the top
    level of exc.response (checked against botocore with moto). A mock - not DynamoDB."""

    def __init__(self, codes=None, items=None):
        self.codes, self.items, self.requests = codes, items or {}, []

    def transact_write_items(self, **kw):
        self.requests.append(kw)
        response = {"Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"}}
        if self.codes is not None:
            response["CancellationReasons"] = [{"Code": c} for c in self.codes]
        raise ClientError(response, "TransactWriteItems")

    def get_item(self, TableName, Key, ConsistentRead=True):  # noqa: N803 - boto3 signature
        item = self.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        return {"Item": item} if item else {}


@pytest.fixture(scope="module")
def captured_calls():
    """Real arguments of every multi-record write, captured from an in-memory fixture flow."""
    calls = {}
    repo = LocalProposalRepository()
    for method in ("create", "record_decision", "start_execution", "append_execution_event", "finish_execution",
                   "record_abort", "append_transition"):
        real = getattr(repo, method)

        def recorder(*a, _real=real, _name=method, **kw):
            calls.setdefault(_name, (a, kw))
            return _real(*a, **kw)
        setattr(repo, method, recorder)
    w = World(repo)
    w.execute()
    calls["_lease"] = repo.get_lease(w.proposal.proposal_id, w.proposals.head(w.proposal.proposal_id).execution_id)
    w2 = World(LocalProposalRepository())
    with pytest.raises(SimulatedCrash):
        w2.execute(hooks=_crash_at("before_write", TARGET))
    eid = w2.proposals.head(w2.proposal.proposal_id).execution_id
    real_abort = w2.repo.record_abort
    w2.repo.record_abort = lambda *a, **kw: (calls.setdefault("record_abort", (a, kw)), real_abort(*a, **kw))[1]
    w2.svc("bob").abort(w2.proposal.proposal_id, content_hash=w2.proposal.content_hash, execution_id=eid,
                        nonce=new_nonce(), idempotency_key="key-capture-ab", reason="x")
    w3 = World(LocalProposalRepository())
    w3.clock.now += timedelta(hours=2)
    real_transition = w3.repo.append_transition
    w3.repo.append_transition = lambda *a, **kw: (calls.setdefault("append_transition", (a, kw)),
                                                  real_transition(*a, **kw))[1]
    w3.proposals.get(w3.proposal.proposal_id)
    return calls


def call(captured_calls, method, codes, items=None):
    items = dict(items or {})
    if method in ("append_execution_event", "finish_execution"):  # these read the lease before the transaction
        lease = captured_calls["_lease"]
        items[(f"PROPOSAL#{lease.proposal_id}", f"EXEC#{lease.execution_id}#LEASE")] =             DynamoProposalRepository(None, "t")._lease_item(lease)
    client = FakeDynamo(codes, items)
    repo = DynamoProposalRepository(client, "t")
    a, kw = captured_calls[method]
    try:
        getattr(repo, method)(*a, **kw)
    except BaseException as exc:  # noqa: BLE001
        return exc, client
    raise AssertionError("expected the mocked transaction to be cancelled")


def idem_item(record: IdempotencyRecord):
    return {(f"IDEMP#{record.scope}#{record.key}", "META"): DynamoProposalRepository._idem_item(record)}


def test_role_order_matches_the_real_transaction_items(captured_calls):
    """The role tuples used to read CancellationReasons must line up with the request's items."""
    expected = {
        "create": ["PROPOSAL#", "PROPOSAL#", "PROPOSAL#", "IDEMP#"],
        "record_decision": ["PROPOSAL#:HEAD", "PROPOSAL#:EVENT#", "PROPOSAL#:DECISION#", "NONCE#", "IDEMP#"],
        "start_execution": ["PROPOSAL#:HEAD", "PROPOSAL#:EVENT#", "PROPOSAL#:EXEC#", "PROPOSAL#:EXEC#", "IDEMP#"],
        "append_execution_event": ["PROPOSAL#:EXEC#", "PROPOSAL#:EXEC#"],
        "finish_execution": ["PROPOSAL#:HEAD", "PROPOSAL#:EVENT#", "PROPOSAL#:EXEC#", "PROPOSAL#:EXEC#"],
        "record_abort": ["PROPOSAL#:HEAD", "PROPOSAL#:EVENT#", "PROPOSAL#:DECISION#", "NONCE#", "IDEMP#",
                         "PROPOSAL#:EXEC#", "PROPOSAL#:EXEC#"],
    }
    for method, prefixes in expected.items():
        _, client = call(captured_calls, method, None)
        items = client.requests[0]["TransactItems"]
        assert len(items) == len(prefixes), method
        for item, want in zip(items, prefixes):
            body = item.get("Put", {}).get("Item") or item.get("Update", {}).get("Key")
            pk, sk = body["PK"]["S"], body["SK"]["S"]
            prefix, _, sk_prefix = want.partition(":")
            assert pk.startswith(prefix) and sk.startswith(sk_prefix), (method, pk, sk, want)


def test_decision_cancellations_map_to_precise_errors(captured_calls):
    idem = captured_calls["record_decision"][0][5]
    cases = [
        (["None", "None", "None", "ConditionalCheckFailed", "None"], NonceReplayError, None),
        (["None", "None", "None", "ConditionalCheckFailed", "ConditionalCheckFailed"], IdempotencyKeyTakenError,
         idem_item(idem)),
        (["ConditionalCheckFailed", "None", "None", "None", "None"], TransitionConflictError, None),
        (["TransactionConflict", "None", "None", "None", "None"], TransientStoreError, None),
        (["None", "ThrottlingError", "None", "None", "None"], TransientStoreError, None),
    ]
    for codes, error, items in cases:
        exc, _ = call(captured_calls, "record_decision", codes, items)
        assert type(exc) is error or isinstance(exc, error), (codes, exc)
    exc, _ = call(captured_calls, "record_decision", ["ValidationError", "None", "None", "None", "None"])
    assert isinstance(exc, ClientError)  # unexpected causes are surfaced, not disguised as conflicts
    exc, _ = call(captured_calls, "record_decision", None)  # no reasons: fall back to follow-up reads
    assert type(exc) is TransitionConflictError


def test_create_and_execution_cancellations_map_to_precise_errors(captured_calls):
    create_idem = captured_calls["create"][0][3]
    assert isinstance(call(captured_calls, "create", ["None", "None", "None", "ConditionalCheckFailed"],
                           idem_item(create_idem))[0], IdempotencyKeyTakenError)
    assert isinstance(call(captured_calls, "create", ["ConditionalCheckFailed", "None", "None", "None"])[0],
                      DuplicateProposalError)
    assert isinstance(call(captured_calls, "create", ["None", "None", "ThrottlingError", "None"])[0], TransientStoreError)

    exc, _ = call(captured_calls, "append_execution_event", ["ConditionalCheckFailed", "None"])
    assert isinstance(exc, LeaseConflictError) and "no longer current" in str(exc)
    exc, _ = call(captured_calls, "append_execution_event", ["None", "ConditionalCheckFailed"])
    assert isinstance(exc, LeaseConflictError) and "already exists" in str(exc)
    assert isinstance(call(captured_calls, "append_execution_event", ["TransactionConflict", "None"])[0],
                      TransientStoreError)

    exc, _ = call(captured_calls, "finish_execution", ["ConditionalCheckFailed", "None", "None", "None"])
    assert isinstance(exc, LeaseConflictError) and "head or events changed" in str(exc)
    exc, _ = call(captured_calls, "finish_execution", ["None", "None", "ConditionalCheckFailed", "None"])
    assert isinstance(exc, LeaseConflictError) and "lease or head changed" in str(exc)

    exc, _ = call(captured_calls, "start_execution", ["None", "None", "None", "ConditionalCheckFailed", "None"])
    assert type(exc) is TransitionConflictError
    start_args = captured_calls["start_execution"][0]
    event, expected = start_args[0], start_args[1]
    expired_head = expected.model_copy(update={"approval_expires_at": event.at})  # same seq, approval no longer valid
    head_items = {(f"PROPOSAL#{expected.proposal_id}", "HEAD"): DynamoProposalRepository._head_item(expired_head)}
    exc, _ = call(captured_calls, "start_execution", ["ConditionalCheckFailed", "None", "None", "None", "None"], head_items)
    assert isinstance(exc, ApprovalNoLongerValidError)

    abort_codes = ["None"] * 7
    assert isinstance(call(captured_calls, "record_abort", abort_codes[:3] + ["ConditionalCheckFailed"] + abort_codes[4:])[0],
                      NonceReplayError)
    assert type(call(captured_calls, "record_abort", abort_codes[:5] + ["ConditionalCheckFailed", "None"])[0]) \
        is TransitionConflictError
    assert isinstance(call(captured_calls, "record_abort", ["TransactionConflict"] + abort_codes[1:])[0], TransientStoreError)
    assert isinstance(call(captured_calls, "append_transition", ["None", "TransactionConflict"])[0], TransientStoreError)
    assert type(call(captured_calls, "append_transition", ["ConditionalCheckFailed", "None"])[0]) is TransitionConflictError


# ================================================================ M4 cleanup (acceptance review L-a .. L-d)


def test_sqlite_write_lock_timeout_is_a_retryable_store_error(tmp_path, monkeypatch):
    """L-a: a busy write lock surfaces as TransientStoreError (cause preserved), and nothing is written."""
    import sqlite3

    from cloudguard.governance.remediation.repository import _SqliteFiles
    monkeypatch.setattr(_SqliteFiles, "BUSY_TIMEOUT_S", 0.2)
    repo = LocalProposalRepository(tmp_path / "store")
    holder = sqlite3.connect(tmp_path / "store" / LocalProposalRepository.DB_FILENAME, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")  # another writer holds the lock
        with pytest.raises(TransientStoreError) as caught:
            with repo.files.lock("x"):
                repo.files.create_exclusive("probe/record.json", "{}")
        assert isinstance(caught.value, TransitionConflictError)  # same retryable family as DynamoDB
        assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        assert "locked" in str(caught.value.__cause__)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert repo.files.read("probe/record.json") is None
    with repo.files.lock("x"):  # the store is usable again once the lock is released
        repo.files.create_exclusive("probe/record.json", "{}")
    assert repo.files.read("probe/record.json") == "{}"
    repo.close()


def test_sqlite_errors_other_than_busy_are_not_disguised(tmp_path):
    """L-a: only SQLITE_BUSY/SQLITE_LOCKED become TransientStoreError."""
    import sqlite3

    from cloudguard.governance.remediation.repository import _sqlite_busy
    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorcode = 5
    locked = sqlite3.OperationalError("database table is locked")
    locked.sqlite_errorcode = 262  # SQLITE_LOCKED_SHAREDCACHE: primary code 6
    other = sqlite3.OperationalError("disk I/O error")
    other.sqlite_errorcode = 10
    assert _sqlite_busy(busy) and _sqlite_busy(locked) and not _sqlite_busy(other)


@pytest.mark.parametrize("write", ["create_exclusive", "replace", "delete"])
def test_sqlite_refuses_writes_inside_a_read_snapshot(tmp_path, write):
    """L-c: a write inside snapshot() is refused instead of silently upgrading the read transaction."""
    repo = LocalProposalRepository(tmp_path / "store")
    with repo.files.lock("x"):
        repo.files.create_exclusive("probe/existing.json", "old")
    args = {"create_exclusive": ("probe/new.json", "x"), "replace": ("probe/existing.json", "new"),
            "delete": ("probe/existing.json",)}[write]
    with repo.read_snapshot():
        with pytest.raises(RuntimeError, match="can't write inside a read snapshot"):
            getattr(repo.files, write)(*args)
    assert repo.files.read("probe/new.json") is None
    assert repo.files.read("probe/existing.json") == "old"
    with repo.files.lock("x"):  # writes inside lock() still work
        repo.files.create_exclusive("probe/new.json", "x")
    assert repo.files.read("probe/new.json") == "x"
    repo.close()


@pytest.mark.parametrize("reasons", [
    ["oops", None],
    [{"Code": "ConditionalCheckFailed"}, 7],
    [{"Code": 5}, {"Code": "None"}],
    [{"Code": ["ConditionalCheckFailed"]}, None],
])
def test_malformed_cancellation_reasons_fall_back_to_inference(reasons):
    """L-b: malformed CancellationReasons entries never raise AttributeError; they mean 'unknown'."""
    from cloudguard.governance.remediation.repository import _cancellation
    exc = ClientError({"Error": {"Code": "TransactionCanceledException"}, "CancellationReasons": reasons}, "T")
    assert _cancellation(exc, ("head", "event")) is None


def test_well_formed_cancellation_reasons_still_map():
    """L-b: None entries and missing codes still count as 'no failure' for that item."""
    from cloudguard.governance.remediation.repository import _cancellation
    exc = ClientError({"Error": {"Code": "TransactionCanceledException"},
                       "CancellationReasons": [None, {}, {"Code": "ConditionalCheckFailed"}]}, "T")
    c = _cancellation(exc, ("head", "event", "nonce"))
    assert c is not None and c.failed == {"nonce"} and c.transient == () and c.other == ()


def test_malformed_cancellation_reasons_use_the_follow_up_read_inference(captured_calls):
    """L-b end to end: record_decision with malformed reasons behaves like 'no reasons' (follow-up reads)."""
    malformed = FakeDynamo(None)
    malformed.reasons = ["oops", None, 3, {"Code": 1}, {}]
    real = malformed.transact_write_items

    def raise_malformed(**kw):
        try:
            real(**kw)
        except ClientError as exc:
            exc.response["CancellationReasons"] = malformed.reasons
            raise
    malformed.transact_write_items = raise_malformed
    a, kw = captured_calls["record_decision"]
    with pytest.raises(TransitionConflictError) as caught:
        DynamoProposalRepository(malformed, "t").record_decision(*a, **kw)
    assert type(caught.value) is TransitionConflictError  # same as the no-reasons fallback


def test_create_comment_describes_the_transaction_not_orphans():
    """L-d: the outdated 'orphaned proposal' comment is gone."""
    import inspect
    source = inspect.getsource(LocalProposalRepository.create)
    assert "orphaned proposal" not in source
    assert "one transaction" in source
