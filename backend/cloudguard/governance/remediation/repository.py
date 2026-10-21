"""Proposal persistence: immutable bodies, append-only lifecycle events, materialised state heads.

Follows the Phase 2 policy-store pattern (Protocol + Local + DynamoDB + manager) without sharing
code with PolicyStore.

DynamoDB single-table layout (see docs/governance-persistence.md):
    PROPOSAL#<id>        META                 immutable proposal body       (no TTL)
    PROPOSAL#<id>        HEAD                 materialised current state    (no TTL)
    PROPOSAL#<id>        EVENT#<10-digit seq> append-only lifecycle event   (no TTL)
    PROPOSAL#<id>        DECISION#<id>        append-only decision record   (no TTL)   [4.2]
    PROPOSAL#<id>        EXEC#<eid>#META      immutable execution record    (no TTL)   [4.3]
    PROPOSAL#<id>        EXEC#<eid>#LEASE     execution lease (fencing)     (no TTL)   [4.3]
    PROPOSAL#<id>        EXEC#<eid>#EVT#<seq> append-only execution event   (no TTL)   [4.3]
    NONCE#<nonce>        USED                 single-use decision nonce     (ttl_epoch hint) [4.2]
    IDEMP#<scope>#<key>  META                 idempotency record            (ttl_epoch hint)

ttl_epoch is a retention hint only: the governance table has NO DynamoDB TTL (writers can set any
attribute, so TTL would let them delete records). Nothing here is ever deleted automatically.

Operations: create, read, history, lazy expiry (4.1); approve, reject, revoke (4.2, via
remediation.approval); fixture execution records (4.3).

Local storage (M4): on disk, records live in ONE SQLite database (`proposals.sqlite3` in the store
directory, WAL mode, synchronous=FULL) under the same logical names and JSON bodies as before. Every
multi-record operation runs in one SQLite transaction, so it commits fully or not at all - also when
the process is killed - and multi-record reads run in one read transaction (a consistent snapshot).
SQLite releases its locks when a process dies, so no stale lock file can block later operations.
The database must live on a local disk: network shares and synced folders (OneDrive, Dropbox, SMB)
are unsupported because SQLite's locking and WAL shared memory are unreliable there.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from botocore.exceptions import ClientError
from pydantic import ValidationError

from cloudguard.governance.custom_tagging.models import TaggingProposal
from cloudguard.governance.models import PolicyRef
from cloudguard.governance.policy_store import PolicyManager
from cloudguard.governance.remediation.audit import verify_decisions, verify_head
from cloudguard.governance.remediation.lifecycle import EXPIRABLE_STATES, event_type_for
from cloudguard.governance.remediation.models import (
    PROPOSAL_ID_PATTERN,
    SYSTEM_EXPIRY_ACTOR,
    Actor,
    CreateResult,
    IdempotencyRecord,
    IdentityEvidence,
    LifecycleEvent,
    ProposalHead,
    ProposalIntegrityError,
    ProposalState,
    ProposalView,
    StoredProposal,
    DecisionRecord,
    NonceRecord,
    request_fingerprint,
)
from cloudguard.governance.remediation.execution_models import ExecutionEvent, ExecutionLease, ExecutionRecord

IDEMPOTENCY_SCOPE = "tagging-proposal-create"
# Retention hint written as ttl_epoch on idempotency records (never deleted automatically: the table
# has no TTL). It must exceed the life of every proposal that can reference them (at most 1 hour).
IDEMPOTENCY_RETENTION = timedelta(days=7)
MAX_PROPOSAL_BYTES = 350_000  # stays under DynamoDB's 400 KB item limit with metadata


class ProposalNotFoundError(LookupError):
    pass


class ProposalNotApprovableError(ValueError):
    """Only approvable, unexpired proposals are stored."""


class ProposalTooLargeError(ValueError):
    pass


class IdempotencyConflictError(ValueError):
    """The idempotency key was already used for a different request."""


class DuplicateProposalError(RuntimeError):
    """A proposal with this id already exists (stored records are never overwritten)."""


class IdempotencyKeyTakenError(RuntimeError):
    """Raised by repositories when the idempotency record already exists."""

    def __init__(self, record: IdempotencyRecord):
        self.record = record
        super().__init__(f"idempotency key already used for {record.proposal_id}")


class TransitionConflictError(RuntimeError):
    """The state head changed since it was read (optimistic concurrency)."""


class NonceReplayError(PermissionError):
    """A decision nonce was already used."""


class TransientStoreError(TransitionConflictError):
    """DynamoDB cancelled a transaction for a transient reason (a conflicting in-flight transaction or
    throttling), or the local SQLite store's write lock stayed busy past its timeout. Nothing was
    written; the operation can be retried."""


class ConsistentReadError(TransitionConflictError):
    """Concurrent commits kept changing the records during a multi-record read, so no single
    committed state could be read. Retryable - this is NOT an integrity (tampering) finding."""


class LeaseConflictError(RuntimeError):
    """Another executor holds (or took over) the execution lease, or the lease expired, was released or
    was revoked by an abort; this writer is fenced out."""


class ApprovalNoLongerValidError(TransitionConflictError):
    """The head no longer holds the expected, unexpired approval (Batch 2 / L2)."""


def _ms(value: datetime) -> int:
    """Epoch milliseconds: the numeric form DynamoDB condition expressions compare."""
    return int(value.timestamp() * 1000)


def _check_id(proposal_id: str) -> str:
    if not PROPOSAL_ID_PATTERN.match(proposal_id):
        raise ProposalNotFoundError(f"invalid proposal id {proposal_id!r}")
    return proposal_id


class ProposalRepository(Protocol):
    def read_snapshot(self) -> contextlib.AbstractContextManager: ...
    def create(self, stored: StoredProposal, head: ProposalHead, event: LifecycleEvent,
               idempotency: IdempotencyRecord) -> None: ...
    def get_stored(self, proposal_id: str) -> StoredProposal | None: ...
    def get_head(self, proposal_id: str) -> ProposalHead | None: ...
    def list_events(self, proposal_id: str) -> list[LifecycleEvent]: ...
    def get_idempotency(self, scope: str, key: str) -> IdempotencyRecord | None: ...
    def append_transition(self, event: LifecycleEvent, expected: ProposalHead, new_head: ProposalHead) -> None: ...
    def repair_head(self, head: ProposalHead, expected_seq: int) -> None: ...
    def record_decision(self, event: LifecycleEvent, expected: ProposalHead, new_head: ProposalHead,
                        decision: DecisionRecord, nonce: NonceRecord, idempotency: IdempotencyRecord) -> None: ...
    def get_decision(self, proposal_id: str, decision_id: str) -> DecisionRecord | None: ...
    def list_decisions(self, proposal_id: str) -> list[DecisionRecord]: ...
    def get_nonce(self, nonce: str) -> NonceRecord | None: ...
    # ---- execution (Checkpoint 4.3)
    def start_execution(self, event: LifecycleEvent, expected: ProposalHead, new_head: ProposalHead,
                        record: ExecutionRecord, lease: ExecutionLease, idempotency: IdempotencyRecord) -> None: ...
    def get_execution(self, proposal_id: str, execution_id: str) -> ExecutionRecord | None: ...
    def list_executions(self, proposal_id: str) -> list[ExecutionRecord]: ...
    def get_lease(self, proposal_id: str, execution_id: str) -> ExecutionLease | None: ...
    def take_over_lease(self, expected: ExecutionLease, new: ExecutionLease) -> None: ...
    def append_execution_event(self, event: ExecutionEvent, lease_token: int, lease_expires_at: datetime,
                               valid_after: datetime | None = None) -> None: ...
    def list_execution_events(self, proposal_id: str, execution_id: str) -> list[ExecutionEvent]: ...
    def finish_execution(self, event: LifecycleEvent, expected: ProposalHead, new_head: ProposalHead,
                         final: ExecutionEvent, lease_token: int) -> None: ...
    def record_abort(self, event: LifecycleEvent, expected: ProposalHead, new_head: ProposalHead,
                     decision: DecisionRecord, nonce: NonceRecord, idempotency: IdempotencyRecord,
                     abort_event: ExecutionEvent, expected_lease: ExecutionLease, new_lease: ExecutionLease) -> None: ...


# ===================================================================== local storage


class _MemoryFiles:
    """Dict-backed record namespace (tests and ephemeral fixture runs).

    lock() is a transaction: the outermost block snapshots the data and restores it if the block
    raises (including BaseException, e.g. a simulated crash), so multi-record writes are
    all-or-nothing here too. snapshot() gives readers a consistent view (writers hold the lock)."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self._lock = threading.RLock()
        self._depth = 0

    def create_exclusive(self, name: str, text: str) -> None:
        with self._lock:
            if name in self.data:
                raise FileExistsError(name)
            self.data[name] = text

    def replace(self, name: str, text: str) -> None:
        with self._lock:
            self.data[name] = text

    def delete(self, name: str) -> None:
        with self._lock:
            self.data.pop(name, None)

    def read(self, name: str) -> str | None:
        return self.data.get(name)

    def list(self, prefix: str) -> list[str]:
        with self._lock:
            return sorted(n for n in self.data if n.startswith(prefix))

    @contextlib.contextmanager
    def lock(self, name: str) -> Iterator[None]:
        with self._lock:
            outermost = self._depth == 0
            saved = dict(self.data) if outermost else None
            self._depth += 1
            try:
                yield
            except BaseException:
                if outermost:
                    self.data.clear()
                    self.data.update(saved)
                raise
            finally:
                self._depth -= 1

    @contextlib.contextmanager
    def snapshot(self) -> Iterator[None]:
        with self._lock:
            yield


class _DiskFiles:
    """Directory-backed namespace with atomic writes.

    - create_exclusive: write a temp file, then hard-link it into place; the link fails if the
      target exists, so an existing record is never overwritten, and readers never see a
      partially written file.
    - replace: write a temp file, then os.replace (atomic on the same volume).
    - lock: an O_EXCL lock file, so separate processes serialise too.
    """

    LOCK_TIMEOUT_S = 10.0

    def __init__(self, root: Path):
        # Create and resolve the root once: resolving a directory before and after it exists can
        # yield different spellings on Windows (e.g. 8.3 short names), breaking the check below.
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)
        self._thread_lock = threading.RLock()

    _SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")

    def _path(self, name: str) -> Path:
        # Lexical check, not Path.resolve(): resolving a file that another thread is deleting can
        # return a different path form on Windows. Names are generated internally from validated
        # ids; this rejects anything that could leave the root regardless.
        if not self._SAFE_NAME.match(name) or any(part in ("", ".", "..") for part in name.split("/")):
            raise ValueError(f"unsafe repository path: {name!r}")
        return self.root.joinpath(*name.split("/"))

    def _write_temp(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        return Path(tmp)

    def create_exclusive(self, name: str, text: str) -> None:
        path = self._path(name)
        tmp = self._write_temp(path, text)
        try:
            os.link(tmp, path)  # atomic; raises FileExistsError if the record exists
        finally:
            tmp.unlink(missing_ok=True)

    def replace(self, name: str, text: str) -> None:
        path = self._path(name)
        tmp = self._write_temp(path, text)
        try:
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def read(self, name: str) -> str | None:
        path = self._path(name)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def list(self, prefix: str) -> list[str]:
        directory = self._path(prefix).parent  # prefixes are "<dir>/<file-prefix>"
        if not directory.exists():
            return []
        rel = [str(p.relative_to(self.root)).replace(os.sep, "/") for p in directory.iterdir() if p.is_file()]
        return sorted(n for n in rel if n.startswith(prefix) and not Path(n).name.startswith(".tmp-"))

    @contextlib.contextmanager
    def lock(self, name: str) -> Iterator[None]:
        lock_path = self._path(f"locks/{name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.LOCK_TIMEOUT_S
        with self._thread_lock:
            while True:
                try:
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    break
                except FileExistsError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"could not acquire {lock_path}; remove it if no CloudGuard process holds it")
                    time.sleep(0.01)
            try:
                os.close(fd)
                yield
            finally:
                lock_path.unlink(missing_ok=True)


_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


def _check_name(name: str) -> str:
    if not _SAFE_NAME.match(name) or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError(f"unsafe repository path: {name!r}")
    return name


class StoreUnavailableError(RuntimeError):
    """The local SQLite store can't be used safely (e.g. WAL unavailable on this filesystem)."""


_SQLITE_BUSY_CODES = frozenset({5, 6})  # SQLITE_BUSY, SQLITE_LOCKED (primary result codes)


def _sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in _SQLITE_BUSY_CODES
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


class _SqliteFiles:
    """SQLite-backed record namespace for the local proposal store (M4).

    Same interface as the old one-file-per-record store - create_exclusive, replace, read, list,
    delete - over a single table `records(name PRIMARY KEY, body)`, so record names and JSON bodies
    are unchanged.

    - lock(name) is a WRITE TRANSACTION (BEGIN IMMEDIATE ... COMMIT). Nested lock() calls join the
      outermost one. Any exception - including BaseException such as a simulated crash - rolls it
      back. A killed process leaves an uncommitted WAL tail that SQLite discards, and its locks are
      released by the OS, so nothing has to be cleaned up by hand.
    - snapshot() is a READ TRANSACTION: every read inside it sees one committed state.
    - Durability: journal_mode=WAL + synchronous=FULL, so a commit is on disk before it returns
      (synchronous=NORMAL would be faster but may lose the latest commits on power loss).
    - Connections are per thread; waits for the write lock are bounded by BUSY_TIMEOUT_S, after
      which TransientStoreError is raised (nothing was written; retry).
    - Writes (create_exclusive, replace, delete) are refused inside a read snapshot.
    - Local disks only: network shares and synced folders are unsupported (WAL needs working
      shared memory and byte-range locks). If WAL can't be enabled, the store refuses to open.
    """

    BUSY_TIMEOUT_S = 30.0

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._conn().execute("CREATE TABLE IF NOT EXISTS records (name TEXT PRIMARY KEY, body TEXT NOT NULL) "
                             "WITHOUT ROWID")

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=self.BUSY_TIMEOUT_S, isolation_level=None)
            conn.execute(f"PRAGMA busy_timeout = {int(self.BUSY_TIMEOUT_S * 1000)}")
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                conn.close()
                raise StoreUnavailableError(f"SQLite WAL mode is unavailable for {self.path} (got {mode!r}); keep the "
                                            "proposal store on a local disk, not a network share or synced folder")
            conn.execute("PRAGMA synchronous = FULL")
            self._local.conn, self._local.depth, self._local.mode = conn, 0, None
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    # ---- records

    def _writer(self) -> sqlite3.Connection:
        conn = self._conn()
        if self._local.mode == "read":
            raise RuntimeError("can't write inside a read snapshot; writes must run inside lock()")
        return conn

    def create_exclusive(self, name: str, text: str) -> None:
        try:
            self._writer().execute("INSERT INTO records (name, body) VALUES (?, ?)", (_check_name(name), text))
        except sqlite3.IntegrityError:
            raise FileExistsError(name) from None

    def replace(self, name: str, text: str) -> None:
        self._writer().execute("INSERT INTO records (name, body) VALUES (?, ?) "
                               "ON CONFLICT(name) DO UPDATE SET body = excluded.body", (_check_name(name), text))

    def delete(self, name: str) -> None:
        self._writer().execute("DELETE FROM records WHERE name = ?", (_check_name(name),))

    def read(self, name: str) -> str | None:
        row = self._conn().execute("SELECT body FROM records WHERE name = ?", (_check_name(name),)).fetchone()
        return row[0] if row else None

    def list(self, prefix: str) -> list[str]:
        rows = self._conn().execute("SELECT name FROM records WHERE substr(name, 1, ?) = ? ORDER BY name",
                                    (len(prefix), _check_name(prefix))).fetchall()
        return [r[0] for r in rows]

    # ---- transactions

    def _commit(self, conn: sqlite3.Connection) -> None:
        conn.execute("COMMIT")

    @contextlib.contextmanager
    def lock(self, name: str) -> Iterator[None]:
        conn = self._conn()
        local = self._local
        if local.depth:
            if local.mode != "write":
                raise RuntimeError("can't write inside a read snapshot")
            local.depth += 1
            try:
                yield
            finally:
                local.depth -= 1
            return
        try:
            conn.execute("BEGIN IMMEDIATE")  # take the write lock up front: checks and writes see one state
        except sqlite3.OperationalError as exc:
            if _sqlite_busy(exc):
                raise TransientStoreError(f"the local proposal store is locked by another writer (waited up to "
                                          f"{self.BUSY_TIMEOUT_S:g} s); nothing was written, retry") from exc
            raise
        local.depth, local.mode = 1, "write"
        try:
            yield
            self._commit(conn)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            local.depth, local.mode = 0, None

    @contextlib.contextmanager
    def snapshot(self) -> Iterator[None]:
        conn = self._conn()
        local = self._local
        if local.depth:  # already inside a transaction on this thread: same consistent view
            yield
            return
        conn.execute("BEGIN")  # deferred: the first read fixes the snapshot for the whole block
        local.depth, local.mode = 1, "read"
        try:
            yield
        finally:
            local.depth, local.mode = 0, None
            if conn.in_transaction:
                conn.execute("COMMIT")

    def close(self) -> None:
        with self._conns_lock:
            for conn in self._conns:
                try:
                    conn.close()
                except sqlite3.ProgrammingError:  # pragma: no cover - closed from its own thread already
                    pass
            self._conns.clear()
        self._local = threading.local()


def _idem_name(scope: str, key: str) -> str:
    return "idempotency/" + hashlib.sha256(f"{scope}#{key}".encode()).hexdigest() + ".json"


def _nonce_name(nonce: str) -> str:
    return "nonces/" + hashlib.sha256(nonce.encode()).hexdigest() + ".json"


_DECISION_ID = re.compile(r"^decision-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _check_decision_id(decision_id: str) -> str:
    if not _DECISION_ID.match(decision_id):
        raise ProposalNotFoundError(f"invalid decision id {decision_id!r}")
    return decision_id


_EXECUTION_ID = re.compile(r"^execution-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _check_execution_id(execution_id: str) -> str:
    if not _EXECUTION_ID.match(execution_id):
        raise ProposalNotFoundError(f"invalid execution id {execution_id!r}")
    return execution_id


class LocalProposalRepository:
    """Local storage for fixture mode and development: one SQLite database in `directory`
    (M4), or in memory when `directory` is None. Every multi-record write runs in one transaction."""

    DB_FILENAME = "proposals.sqlite3"

    def __init__(self, directory: str | Path | None = None):
        self.files = _SqliteFiles(Path(directory) / self.DB_FILENAME) if directory else _MemoryFiles()

    def read_snapshot(self):
        """A consistent read view for multi-record reads."""
        return self.files.snapshot()

    def close(self) -> None:
        close = getattr(self.files, "close", None)
        if close:
            close()

    @staticmethod
    def _base(proposal_id: str) -> str:
        return f"proposals/{_check_id(proposal_id)}/"

    def _parse(self, name: str, model):
        text = self.files.read(name)
        if text is None:
            return None
        try:
            return model.model_validate_json(text)
        except ValidationError as exc:
            raise ProposalIntegrityError(f"corrupt record {name}: {exc}") from None

    def create(self, stored, head, event, idempotency) -> None:
        base = self._base(stored.proposal_id)
        idem = _idem_name(idempotency.scope, idempotency.key)
        with self.files.lock("create"):
            existing = self._parse(idem, IdempotencyRecord)
            if existing is not None:
                raise IdempotencyKeyTakenError(existing)
            try:
                self.files.create_exclusive(base + "META.json", stored.model_dump_json())
            except FileExistsError:
                raise DuplicateProposalError(stored.proposal_id) from None
            self.files.create_exclusive(base + f"EVENT-{event.seq:010d}.json", event.model_dump_json())
            self.files.replace(base + "HEAD.json", head.model_dump_json())
            # All four records commit in one transaction (SQLite) or roll back together (memory), so a
            # crash leaves either the whole proposal with its idempotency record or nothing at all.
            self.files.create_exclusive(idem, idempotency.model_dump_json())

    def get_stored(self, proposal_id):
        return self._parse(self._base(proposal_id) + "META.json", StoredProposal)

    def get_head(self, proposal_id):
        return self._read_head(proposal_id)

    def _read_head(self, proposal_id):
        # Internal guards read storage through this private method, so overriding the public
        # accessor can never bypass a concurrency check.
        return self._parse(self._base(proposal_id) + "HEAD.json", ProposalHead)

    def list_events(self, proposal_id):
        base = self._base(proposal_id)
        return [self._parse(n, LifecycleEvent) for n in self.files.list(base + "EVENT-")]

    def get_idempotency(self, scope, key):
        return self._parse(_idem_name(scope, key), IdempotencyRecord)

    def append_transition(self, event, expected, new_head) -> None:
        base = self._base(event.proposal_id)
        with self.files.lock(event.proposal_id):
            current = self._read_head(event.proposal_id)
            if current is None or (current.seq, current.state, current.content_hash) != \
                    (expected.seq, expected.state, expected.content_hash):
                raise TransitionConflictError(f"head of {event.proposal_id} changed since it was read")
            try:
                self.files.create_exclusive(base + f"EVENT-{event.seq:010d}.json", event.model_dump_json())
            except FileExistsError:
                raise TransitionConflictError(f"event {event.seq} of {event.proposal_id} already exists") from None
            self.files.replace(base + "HEAD.json", new_head.model_dump_json())

    def repair_head(self, head, expected_seq) -> None:
        with self.files.lock(head.proposal_id):
            current = self._read_head(head.proposal_id)
            if current is not None and current.seq == expected_seq < head.seq:
                self.files.replace(self._base(head.proposal_id) + "HEAD.json", head.model_dump_json())

    def record_decision(self, event, expected, new_head, decision, nonce, idempotency) -> None:
        base = self._base(event.proposal_id)
        _check_decision_id(decision.decision_id)
        with self.files.lock("decisions"), self.files.lock(event.proposal_id):
            if self.files.read(_nonce_name(nonce.nonce)) is not None:
                raise NonceReplayError("decision nonce was already used")
            existing = self._parse(_idem_name(idempotency.scope, idempotency.key), IdempotencyRecord)
            if existing is not None:
                raise IdempotencyKeyTakenError(existing)
            current = self._read_head(event.proposal_id)
            if current is None or (current.seq, current.state, current.content_hash) != \
                    (expected.seq, expected.state, expected.content_hash):
                raise TransitionConflictError(f"head of {event.proposal_id} changed since it was read")
            self.files.create_exclusive(base + f"DECISION-{decision.decision_id}.json", decision.model_dump_json())
            try:
                self.files.create_exclusive(base + f"EVENT-{event.seq:010d}.json", event.model_dump_json())
            except FileExistsError:
                raise TransitionConflictError(f"event {event.seq} of {event.proposal_id} already exists") from None
            self.files.replace(base + "HEAD.json", new_head.model_dump_json())
            self.files.create_exclusive(_nonce_name(nonce.nonce), nonce.model_dump_json())
            self.files.create_exclusive(_idem_name(idempotency.scope, idempotency.key), idempotency.model_dump_json())

    def get_decision(self, proposal_id, decision_id):
        return self._parse(self._base(proposal_id) + f"DECISION-{_check_decision_id(decision_id)}.json", DecisionRecord)

    def list_decisions(self, proposal_id):
        return [self._parse(n, DecisionRecord) for n in self.files.list(self._base(proposal_id) + "DECISION-")]

    def get_nonce(self, nonce):
        return self._parse(_nonce_name(nonce), NonceRecord)

    # ---- execution (Checkpoint 4.3)

    def _exec(self, proposal_id: str, execution_id: str) -> str:
        return self._base(proposal_id) + f"EXEC-{_check_execution_id(execution_id)}-"

    def _check_head(self, proposal_id, expected) -> None:
        current = self._read_head(proposal_id)
        if current is None or (current.seq, current.state, current.content_hash) != \
                (expected.seq, expected.state, expected.content_hash):
            raise TransitionConflictError(f"head of {proposal_id} changed since it was read")

    def _check_lease(self, proposal_id, execution_id, token, valid_after: datetime | None = None) -> ExecutionLease:
        """Fencing: the lease must carry `token`, be unreleased and, when `valid_after` is given, still
        be valid after that instant. An expired lease can't be renewed, even before a takeover."""
        lease = self._parse(self._exec(proposal_id, execution_id) + "LEASE.json", ExecutionLease)
        if lease is None or lease.token != token or lease.released:
            raise LeaseConflictError(f"execution {execution_id} lease token {token} is no longer current")
        if valid_after is not None and not lease.expires_at > valid_after:
            raise LeaseConflictError(f"execution {execution_id} lease token {token} expired at {lease.expires_at.isoformat()}")
        return lease

    def start_execution(self, event, expected, new_head, record, lease, idempotency) -> None:
        base, ex = self._base(event.proposal_id), self._exec(record.proposal_id, record.execution_id)
        with self.files.lock("decisions"), self.files.lock(event.proposal_id):
            existing = self._parse(_idem_name(idempotency.scope, idempotency.key), IdempotencyRecord)
            if existing is not None:
                raise IdempotencyKeyTakenError(existing)
            self._check_head(event.proposal_id, expected)
            current = self._read_head(event.proposal_id)
            if current.approval_id != record.approval_id or current.approval_expires_at is None \
                    or not event.at < current.approval_expires_at:
                raise ApprovalNoLongerValidError(f"approval {record.approval_id} is no longer valid at {event.at.isoformat()}")
            self.files.create_exclusive(ex + "META.json", record.model_dump_json())
            self.files.create_exclusive(ex + "LEASE.json", lease.model_dump_json())
            try:
                self.files.create_exclusive(base + f"EVENT-{event.seq:010d}.json", event.model_dump_json())
            except FileExistsError:
                raise TransitionConflictError(f"event {event.seq} of {event.proposal_id} already exists") from None
            self.files.replace(base + "HEAD.json", new_head.model_dump_json())
            self.files.create_exclusive(_idem_name(idempotency.scope, idempotency.key), idempotency.model_dump_json())

    def get_execution(self, proposal_id, execution_id):
        return self._parse(self._exec(proposal_id, execution_id) + "META.json", ExecutionRecord)

    def list_executions(self, proposal_id):
        return [self._parse(n, ExecutionRecord) for n in self.files.list(self._base(proposal_id) + "EXEC-")
                if n.endswith("-META.json")]

    def get_lease(self, proposal_id, execution_id):
        return self._parse(self._exec(proposal_id, execution_id) + "LEASE.json", ExecutionLease)

    def take_over_lease(self, expected, new) -> None:
        with self.files.lock(expected.proposal_id):
            self._check_lease(expected.proposal_id, expected.execution_id, expected.token)
            current = self.get_lease(expected.proposal_id, expected.execution_id)
            if current != expected:
                raise LeaseConflictError("lease changed since it was read")
            if current.expires_at > new.acquired_at:
                raise LeaseConflictError("a lease can only be taken over after it expires")
            self.files.replace(self._exec(new.proposal_id, new.execution_id) + "LEASE.json", new.model_dump_json())

    def append_execution_event(self, event, lease_token, lease_expires_at, valid_after=None) -> None:
        ex = self._exec(event.proposal_id, event.execution_id)
        with self.files.lock(event.proposal_id):
            lease = self._check_lease(event.proposal_id, event.execution_id, lease_token, valid_after or event.at)
            try:
                self.files.create_exclusive(ex + f"EVT-{event.seq:010d}.json", event.model_dump_json())
            except FileExistsError:
                raise LeaseConflictError(f"execution event {event.seq} already exists") from None
            self.files.replace(ex + "LEASE.json", lease.model_copy(update={"expires_at": lease_expires_at}).model_dump_json())

    def list_execution_events(self, proposal_id, execution_id):
        return [self._parse(n, ExecutionEvent) for n in self.files.list(self._exec(proposal_id, execution_id) + "EVT-")]

    def finish_execution(self, event, expected, new_head, final, lease_token) -> None:
        base, ex = self._base(event.proposal_id), self._exec(final.proposal_id, final.execution_id)
        with self.files.lock(event.proposal_id):
            lease = self._check_lease(final.proposal_id, final.execution_id, lease_token, final.at)
            self._check_head(event.proposal_id, expected)
            try:
                self.files.create_exclusive(ex + f"EVT-{final.seq:010d}.json", final.model_dump_json())
                self.files.create_exclusive(base + f"EVENT-{event.seq:010d}.json", event.model_dump_json())
            except FileExistsError:
                raise TransitionConflictError("execution already finalized") from None
            self.files.replace(base + "HEAD.json", new_head.model_dump_json())
            self.files.replace(ex + "LEASE.json", lease.model_copy(update={"released": True}).model_dump_json())

    def record_abort(self, event, expected, new_head, decision, nonce, idempotency, abort_event, expected_lease,
                     new_lease) -> None:
        """Batch 2 (M1): one locked step records the ABORT decision, moves the head EXECUTING -> ABORTED,
        revokes the lease (new token, released) and closes the execution's audit chain."""
        base, ex = self._base(event.proposal_id), self._exec(abort_event.proposal_id, abort_event.execution_id)
        _check_decision_id(decision.decision_id)
        with self.files.lock("decisions"), self.files.lock(event.proposal_id):
            if self.files.read(_nonce_name(nonce.nonce)) is not None:
                raise NonceReplayError("decision nonce was already used")
            existing = self._parse(_idem_name(idempotency.scope, idempotency.key), IdempotencyRecord)
            if existing is not None:
                raise IdempotencyKeyTakenError(existing)
            self._check_head(event.proposal_id, expected)
            if self.get_lease(expected_lease.proposal_id, expected_lease.execution_id) != expected_lease:
                raise TransitionConflictError("the execution lease changed since it was read")
            if self.files.read(ex + f"EVT-{abort_event.seq:010d}.json") is not None:
                raise TransitionConflictError("the execution recorded another event first")
            self.files.create_exclusive(base + f"DECISION-{decision.decision_id}.json", decision.model_dump_json())
            self.files.create_exclusive(ex + f"EVT-{abort_event.seq:010d}.json", abort_event.model_dump_json())
            self.files.create_exclusive(base + f"EVENT-{event.seq:010d}.json", event.model_dump_json())
            self.files.replace(base + "HEAD.json", new_head.model_dump_json())
            self.files.replace(ex + "LEASE.json", new_lease.model_dump_json())
            self.files.create_exclusive(_nonce_name(nonce.nonce), nonce.model_dump_json())
            self.files.create_exclusive(_idem_name(idempotency.scope, idempotency.key), idempotency.model_dump_json())


# ===================================================================== DynamoDB


def _pk(proposal_id: str) -> str:
    return f"PROPOSAL#{_check_id(proposal_id)}"


def _event_sk(seq: int) -> str:
    return f"EVENT#{seq:010d}"


def _idem_key(scope: str, key: str) -> dict:
    return {"PK": {"S": f"IDEMP#{scope}#{key}"}, "SK": {"S": "META"}}


def _cancelled(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in ("TransactionCanceledException", "ConditionalCheckFailedException")


_TRANSIENT_CANCELLATION_CODES = frozenset({"TransactionConflict", "ThrottlingError", "ProvisionedThroughputExceeded",
                                           "RequestLimitExceeded"})


@dataclass(frozen=True)
class _Cancellation:
    failed: frozenset[str]  # roles of the TransactItems entries whose condition check failed
    transient: tuple[str, ...]  # transient cancellation codes (retryable)
    other: tuple[str, ...]  # anything else (validation errors, size limits, unknown codes)


def _cancellation(exc: ClientError, roles: tuple[str, ...]) -> _Cancellation | None:
    """Read DynamoDB's CancellationReasons - one entry per TransactItems entry, in request order -
    from botocore's parsed TransactionCanceledException (exc.response["CancellationReasons"]). Returns
    None when the reasons are absent, malformed or don't line up with `roles`; callers then fall back
    to follow-up reads. Documented codes: None, ConditionalCheckFailed, ItemCollectionSizeLimitExceeded,
    TransactionConflict, ProvisionedThroughputExceeded, ThrottlingError, ValidationError."""
    reasons = exc.response.get("CancellationReasons")
    if not isinstance(reasons, list) or len(reasons) != len(roles):
        return None
    codes = []
    for r in reasons:  # a malformed entry (not a dict, or a non-string Code) means: fall back to inference
        if r is None:
            r = {}
        if not isinstance(r, dict):
            return None
        code = r.get("Code")
        if code is None:
            code = "None"
        if not isinstance(code, str):
            return None
        codes.append(code)
    return _Cancellation(
        failed=frozenset(role for role, code in zip(roles, codes) if code == "ConditionalCheckFailed"),
        transient=tuple(c for c in codes if c in _TRANSIENT_CANCELLATION_CODES),
        other=tuple(c for c in codes if c not in ("None", "ConditionalCheckFailed") and c not in _TRANSIENT_CANCELLATION_CODES),
    )


def _transient(c: _Cancellation, what: str) -> TransientStoreError:
    return TransientStoreError(f"{what} was cancelled by DynamoDB ({', '.join(c.transient)}); nothing was written, retry")


class DynamoProposalRepository:
    """DynamoDB single-table storage. META, HEAD and EVENT items never carry a TTL attribute."""

    def __init__(self, client, table_name: str):
        self.client = client
        self.table = table_name

    def read_snapshot(self):
        """DynamoDB has no multi-item read snapshot; readers use retry-and-compare instead (M4)."""
        return contextlib.nullcontext()

    # ---- serialisation

    @staticmethod
    def _meta_item(s: StoredProposal) -> dict:
        return {
            "PK": {"S": _pk(s.proposal_id)}, "SK": {"S": "META"}, "entity": {"S": "ProposalMeta"},
            "proposal_id": {"S": s.proposal_id}, "content_hash": {"S": s.content_hash},
            "binding_digest": {"S": s.binding_digest}, "schema_version": {"S": s.schema_version},
            "account_id": {"S": s.account_id}, "policy_id": {"S": s.policy.policy_id},
            "policy_version": {"N": str(s.policy.version)}, "policy_hash": {"S": s.policy.content_hash},
            "proposal_expires_at": {"S": s.proposal_expires_at.isoformat()},
            "stored_at": {"S": s.stored_at.isoformat()}, "created_by": {"S": s.created_by.model_dump_json()},
            "idempotency_key": {"S": s.idempotency_key}, "request_fingerprint": {"S": s.request_fingerprint},
            "proposal_json": {"S": s.proposal_json},
            "resource_identities": {"S": json.dumps(s.resource_identities, sort_keys=True)},
            **({"requester_identity": {"S": s.requester_identity.model_dump_json()}} if s.requester_identity else {}),
        }

    @staticmethod
    def _head_item(h: ProposalHead) -> dict:
        return {
            "PK": {"S": _pk(h.proposal_id)}, "SK": {"S": "HEAD"}, "entity": {"S": "ProposalHead"},
            "proposal_id": {"S": h.proposal_id}, "state": {"S": h.state.value}, "seq": {"N": str(h.seq)},
            "content_hash": {"S": h.content_hash}, "proposal_expires_at": {"S": h.proposal_expires_at.isoformat()},
            "updated_at": {"S": h.updated_at.isoformat()},
            **({"approval_id": {"S": h.approval_id}} if h.approval_id else {}),
            **({"approval_expires_at": {"S": h.approval_expires_at.isoformat()},
                "approval_expires_ms": {"N": str(_ms(h.approval_expires_at))}} if h.approval_expires_at else {}),
            **({"execution_id": {"S": h.execution_id}} if h.execution_id else {}),
        }

    @staticmethod
    def _event_item(e: LifecycleEvent) -> dict:
        return {
            "PK": {"S": _pk(e.proposal_id)}, "SK": {"S": _event_sk(e.seq)}, "entity": {"S": "LifecycleEvent"},
            "seq": {"N": str(e.seq)}, "event_type": {"S": e.event_type.value}, "to_state": {"S": e.to_state.value},
            "at": {"S": e.at.isoformat()}, "event_json": {"S": e.model_dump_json()},
        }

    @staticmethod
    def _idem_item(r: IdempotencyRecord) -> dict:
        return {
            **_idem_key(r.scope, r.key), "entity": {"S": "IdempotencyRecord"},
            "record_json": {"S": r.model_dump_json()}, "ttl_epoch": {"N": str(r.ttl_epoch)},
        }

    @staticmethod
    def _load(item: dict, build):
        try:
            return build(item)
        except (KeyError, ValueError, ValidationError) as exc:
            raise ProposalIntegrityError(f"corrupt item {item.get('PK', {}).get('S')} {item.get('SK', {}).get('S')}: {exc}") from None

    def _get(self, key: dict) -> dict | None:
        return self.client.get_item(TableName=self.table, Key=key, ConsistentRead=True).get("Item")

    # ---- protocol

    def create(self, stored, head, event, idempotency) -> None:
        not_exists = "attribute_not_exists(PK)"
        try:
            self.client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table, "Item": self._meta_item(stored), "ConditionExpression": not_exists}},
                {"Put": {"TableName": self.table, "Item": self._head_item(head), "ConditionExpression": not_exists}},
                {"Put": {"TableName": self.table, "Item": self._event_item(event), "ConditionExpression": not_exists}},
                {"Put": {"TableName": self.table, "Item": self._idem_item(idempotency), "ConditionExpression": not_exists}},
            ])
        except ClientError as exc:
            if not _cancelled(exc):
                raise
            c = _cancellation(exc, ("meta", "head", "event", "idempotency"))
            if c is not None and not c.other:
                if "idempotency" in c.failed:
                    existing = self.get_idempotency(idempotency.scope, idempotency.key)
                    if existing is not None:
                        raise IdempotencyKeyTakenError(existing) from None
                if c.failed:
                    raise DuplicateProposalError(stored.proposal_id) from None
                if c.transient:
                    raise _transient(c, "proposal creation") from None
            elif c is not None:
                raise
            existing = self.get_idempotency(idempotency.scope, idempotency.key)
            if existing is not None:
                raise IdempotencyKeyTakenError(existing) from None
            raise DuplicateProposalError(stored.proposal_id) from None

    def get_stored(self, proposal_id):
        item = self._get({"PK": {"S": _pk(proposal_id)}, "SK": {"S": "META"}})
        if item is None:
            return None

        def build(i):
            return StoredProposal(
                proposal_id=i["proposal_id"]["S"], content_hash=i["content_hash"]["S"],
                binding_digest=i["binding_digest"]["S"], schema_version=i["schema_version"]["S"],
                account_id=i["account_id"]["S"],
                policy=PolicyRef(policy_id=i["policy_id"]["S"], version=int(i["policy_version"]["N"]),
                                 content_hash=i["policy_hash"]["S"]),
                proposal_expires_at=datetime.fromisoformat(i["proposal_expires_at"]["S"]),
                stored_at=datetime.fromisoformat(i["stored_at"]["S"]),
                created_by=Actor.model_validate_json(i["created_by"]["S"]),
                idempotency_key=i["idempotency_key"]["S"], request_fingerprint=i["request_fingerprint"]["S"],
                proposal_json=i["proposal_json"]["S"],
                resource_identities=json.loads(i["resource_identities"]["S"]) if "resource_identities" in i else {},
                requester_identity=(IdentityEvidence.model_validate_json(i["requester_identity"]["S"])
                                    if "requester_identity" in i else None),
            )
        return self._load(item, build)

    def get_head(self, proposal_id):
        item = self._get({"PK": {"S": _pk(proposal_id)}, "SK": {"S": "HEAD"}})
        if item is None:
            return None
        return self._load(item, lambda i: ProposalHead(
            proposal_id=i["proposal_id"]["S"], state=ProposalState(i["state"]["S"]), seq=int(i["seq"]["N"]),
            content_hash=i["content_hash"]["S"], proposal_expires_at=datetime.fromisoformat(i["proposal_expires_at"]["S"]),
            updated_at=datetime.fromisoformat(i["updated_at"]["S"]),
            approval_id=i["approval_id"]["S"] if "approval_id" in i else None,
            approval_expires_at=datetime.fromisoformat(i["approval_expires_at"]["S"]) if "approval_expires_at" in i else None,
            execution_id=i["execution_id"]["S"] if "execution_id" in i else None,
        ))

    def list_events(self, proposal_id):
        events = []
        pages = self.client.get_paginator("query").paginate(
            TableName=self.table, ConsistentRead=True,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :e)",
            ExpressionAttributeValues={":pk": {"S": _pk(proposal_id)}, ":e": {"S": "EVENT#"}},
        )
        for page in pages:
            for item in page.get("Items", []):
                event = self._load(item, lambda i: LifecycleEvent.model_validate_json(i["event_json"]["S"]))
                if item["SK"]["S"] != _event_sk(event.seq) or int(item["seq"]["N"]) != event.seq:
                    raise ProposalIntegrityError(f"event item {item['SK']['S']} does not match its content")
                events.append(event)
        return events

    def get_idempotency(self, scope, key):
        item = self._get(_idem_key(scope, key))
        if item is None:
            return None
        return self._load(item, lambda i: IdempotencyRecord.model_validate_json(i["record_json"]["S"]))

    def _head_update(self, event, expected, new_head, extra_condition: str = "", extra_values: dict | None = None) -> dict:
        sets = ["#state = :new_state", "#seq = :new_seq", "updated_at = :updated"]
        values = {
            ":new_state": {"S": new_head.state.value}, ":new_seq": {"N": str(new_head.seq)},
            ":updated": {"S": new_head.updated_at.isoformat()},
            ":expected_seq": {"N": str(expected.seq)}, ":expected_state": {"S": expected.state.value},
            ":hash": {"S": expected.content_hash},
        }
        if new_head.approval_id:
            sets.append("approval_id = :aid")
            values[":aid"] = {"S": new_head.approval_id}
        if new_head.approval_expires_at:
            sets += ["approval_expires_at = :aexp", "approval_expires_ms = :aexpms"]
            values[":aexp"] = {"S": new_head.approval_expires_at.isoformat()}
            values[":aexpms"] = {"N": str(_ms(new_head.approval_expires_at))}
        values.update(extra_values or {})
        if new_head.execution_id:
            sets.append("execution_id = :eid")
            values[":eid"] = {"S": new_head.execution_id}
        return {"Update": {
            "TableName": self.table,
            "Key": {"PK": {"S": _pk(event.proposal_id)}, "SK": {"S": "HEAD"}},
            "UpdateExpression": "SET " + ", ".join(sets),
            "ConditionExpression": "#seq = :expected_seq AND #state = :expected_state AND content_hash = :hash"
                                   + (f" AND {extra_condition}" if extra_condition else ""),
            "ExpressionAttributeNames": {"#state": "state", "#seq": "seq"},
            "ExpressionAttributeValues": values,
        }}

    def append_transition(self, event, expected, new_head) -> None:
        try:
            self.client.transact_write_items(TransactItems=[
                self._head_update(event, expected, new_head),
                {"Put": {"TableName": self.table, "Item": self._event_item(event),
                         "ConditionExpression": "attribute_not_exists(PK)"}},
            ])
        except ClientError as exc:
            if not _cancelled(exc):
                raise
            c = _cancellation(exc, ("head", "event"))
            if c is not None and c.other:
                raise
            if c is not None and not c.failed and c.transient:
                raise _transient(c, "the state transition") from None
            raise TransitionConflictError(f"head of {event.proposal_id} changed since it was read") from None

    def record_decision(self, event, expected, new_head, decision, nonce, idempotency) -> None:
        _check_decision_id(decision.decision_id)
        not_exists = "attribute_not_exists(PK)"
        nonce_key = {"PK": {"S": f"NONCE#{nonce.nonce}"}, "SK": {"S": "USED"}}
        try:
            self.client.transact_write_items(TransactItems=[
                self._head_update(event, expected, new_head),
                {"Put": {"TableName": self.table, "Item": self._event_item(event), "ConditionExpression": not_exists}},
                {"Put": {"TableName": self.table, "ConditionExpression": not_exists, "Item": {
                    "PK": {"S": _pk(decision.proposal_id)}, "SK": {"S": f"DECISION#{decision.decision_id}"},
                    "entity": {"S": "DecisionRecord"}, "decision": {"S": decision.decision.value},
                    "decision_json": {"S": decision.model_dump_json()}}}},
                {"Put": {"TableName": self.table, "ConditionExpression": not_exists, "Item": {
                    **nonce_key, "entity": {"S": "NonceRecord"}, "record_json": {"S": nonce.model_dump_json()},
                    "ttl_epoch": {"N": str(nonce.ttl_epoch)}}}},
                {"Put": {"TableName": self.table, "Item": self._idem_item(idempotency), "ConditionExpression": not_exists}},
            ])
        except ClientError as exc:
            if not _cancelled(exc):
                raise
            c = _cancellation(exc, ("head", "event", "decision", "nonce", "idempotency"))
            if c is not None and not c.other:
                if "idempotency" in c.failed:  # replay of an identical request wins over a nonce collision
                    existing = self.get_idempotency(idempotency.scope, idempotency.key)
                    if existing is not None:
                        raise IdempotencyKeyTakenError(existing) from None
                if "nonce" in c.failed:
                    raise NonceReplayError("decision nonce was already used") from None
                if c.failed:
                    raise TransitionConflictError(f"head of {event.proposal_id} changed since it was read") from None
                if c.transient:
                    raise _transient(c, "the decision") from None
            elif c is not None:
                raise
            if self._get(nonce_key) is not None:
                raise NonceReplayError("decision nonce was already used") from None
            existing = self.get_idempotency(idempotency.scope, idempotency.key)
            if existing is not None:
                raise IdempotencyKeyTakenError(existing) from None
            raise TransitionConflictError(f"head of {event.proposal_id} changed since it was read") from None

    def get_decision(self, proposal_id, decision_id):
        item = self._get({"PK": {"S": _pk(proposal_id)}, "SK": {"S": f"DECISION#{_check_decision_id(decision_id)}"}})
        if item is None:
            return None
        return self._load(item, lambda i: DecisionRecord.model_validate_json(i["decision_json"]["S"]))

    def list_decisions(self, proposal_id):
        out = []
        for page in self.client.get_paginator("query").paginate(
                TableName=self.table, ConsistentRead=True, KeyConditionExpression="PK = :pk AND begins_with(SK, :d)",
                ExpressionAttributeValues={":pk": {"S": _pk(proposal_id)}, ":d": {"S": "DECISION#"}}):
            out += [self._load(i, lambda x: DecisionRecord.model_validate_json(x["decision_json"]["S"]))
                    for i in page.get("Items", [])]
        return out

    def get_nonce(self, nonce):
        item = self._get({"PK": {"S": f"NONCE#{nonce}"}, "SK": {"S": "USED"}})
        if item is None:
            return None
        return self._load(item, lambda i: NonceRecord.model_validate_json(i["record_json"]["S"]))

    # ---- execution (Checkpoint 4.3)

    @staticmethod
    def _exec_sk(execution_id: str, suffix: str) -> str:
        return f"EXEC#{_check_execution_id(execution_id)}#{suffix}"

    def _lease_item(self, lease: ExecutionLease) -> dict:
        return {"PK": {"S": _pk(lease.proposal_id)}, "SK": {"S": self._exec_sk(lease.execution_id, "LEASE")},
                "entity": {"S": "ExecutionLease"}, "token": {"N": str(lease.token)},
                "released": {"BOOL": lease.released}, "expires_ms": {"N": str(_ms(lease.expires_at))},
                "lease_json": {"S": lease.model_dump_json()}}

    def _exec_event_item(self, e: ExecutionEvent) -> dict:
        return {"PK": {"S": _pk(e.proposal_id)}, "SK": {"S": self._exec_sk(e.execution_id, f"EVT#{e.seq:010d}")},
                "entity": {"S": "ExecutionEvent"}, "event_json": {"S": e.model_dump_json()}}

    def _fenced_lease_update(self, proposal_id, execution_id, token, lease_after: ExecutionLease,
                             valid_after: datetime) -> dict:
        """Fencing: token must match, the lease must be unreleased and still valid after `valid_after`
        (the writer's clock; DynamoDB conditions have no server time). An expired lease is never renewed."""
        return {"Update": {
            "TableName": self.table,
            "Key": {"PK": {"S": _pk(proposal_id)}, "SK": {"S": self._exec_sk(execution_id, "LEASE")}},
            "UpdateExpression": "SET lease_json = :json, released = :rel, expires_ms = :exp",
            "ConditionExpression": "#t = :token AND released = :false AND expires_ms > :valid",
            "ExpressionAttributeNames": {"#t": "token"},
            "ExpressionAttributeValues": {":json": {"S": lease_after.model_dump_json()}, ":rel": {"BOOL": lease_after.released},
                                          ":exp": {"N": str(_ms(lease_after.expires_at))},
                                          ":token": {"N": str(token)}, ":false": {"BOOL": False},
                                          ":valid": {"N": str(_ms(valid_after))}},
        }}

    def start_execution(self, event, expected, new_head, record, lease, idempotency) -> None:
        not_exists = "attribute_not_exists(PK)"
        try:
            self.client.transact_write_items(TransactItems=[
                self._head_update(event, expected, new_head,
                                  "approval_id = :aid_expected AND approval_expires_ms > :start_ms",
                                  {":aid_expected": {"S": record.approval_id}, ":start_ms": {"N": str(_ms(event.at))}}),
                {"Put": {"TableName": self.table, "Item": self._event_item(event), "ConditionExpression": not_exists}},
                {"Put": {"TableName": self.table, "ConditionExpression": not_exists, "Item": {
                    "PK": {"S": _pk(record.proposal_id)}, "SK": {"S": self._exec_sk(record.execution_id, "META")},
                    "entity": {"S": "ExecutionRecord"}, "record_json": {"S": record.model_dump_json()}}}},
                {"Put": {"TableName": self.table, "Item": self._lease_item(lease), "ConditionExpression": not_exists}},
                {"Put": {"TableName": self.table, "Item": self._idem_item(idempotency), "ConditionExpression": not_exists}},
            ])
        except ClientError as exc:
            if not _cancelled(exc):
                raise
            c = _cancellation(exc, ("head", "event", "execution", "lease", "idempotency"))
            if c is not None and c.other:
                raise
            if c is not None and not c.failed and c.transient:
                raise _transient(c, "the execution start") from None
            if c is None or "idempotency" in c.failed:
                existing = self.get_idempotency(idempotency.scope, idempotency.key)
                if existing is not None:
                    raise IdempotencyKeyTakenError(existing) from None
            if c is not None and "head" not in c.failed:
                raise TransitionConflictError(f"execution of {event.proposal_id} changed since it was read") from None
            head = self.get_head(event.proposal_id)
            if head is not None and head.seq == expected.seq and (
                    head.approval_id != record.approval_id or head.approval_expires_at is None
                    or not event.at < head.approval_expires_at):
                raise ApprovalNoLongerValidError(f"approval {record.approval_id} is no longer valid") from None
            raise TransitionConflictError(f"head of {event.proposal_id} changed since it was read") from None

    def get_execution(self, proposal_id, execution_id):
        item = self._get({"PK": {"S": _pk(proposal_id)}, "SK": {"S": self._exec_sk(execution_id, "META")}})
        return None if item is None else self._load(item, lambda i: ExecutionRecord.model_validate_json(i["record_json"]["S"]))

    def list_executions(self, proposal_id):
        out = []
        for page in self.client.get_paginator("query").paginate(
                TableName=self.table, ConsistentRead=True, KeyConditionExpression="PK = :pk AND begins_with(SK, :x)",
                ExpressionAttributeValues={":pk": {"S": _pk(proposal_id)}, ":x": {"S": "EXEC#"}}):
            out += [self._load(i, lambda x: ExecutionRecord.model_validate_json(x["record_json"]["S"]))
                    for i in page.get("Items", []) if i["SK"]["S"].endswith("#META")]
        return out

    def get_lease(self, proposal_id, execution_id):
        item = self._get({"PK": {"S": _pk(proposal_id)}, "SK": {"S": self._exec_sk(execution_id, "LEASE")}})
        if item is None:
            return None
        lease = self._load(item, lambda i: ExecutionLease.model_validate_json(i["lease_json"]["S"]))
        if int(item["token"]["N"]) != lease.token or item["released"]["BOOL"] != lease.released:
            raise ProposalIntegrityError("lease item attributes do not match its content")
        return lease

    def take_over_lease(self, expected, new) -> None:
        try:
            self.client.update_item(
                TableName=self.table,
                Key={"PK": {"S": _pk(expected.proposal_id)}, "SK": {"S": self._exec_sk(expected.execution_id, "LEASE")}},
                UpdateExpression="SET #t = :new, lease_json = :json, expires_ms = :exp",
                ConditionExpression="#t = :old AND released = :false AND lease_json = :oldjson AND expires_ms <= :now",
                ExpressionAttributeNames={"#t": "token"},
                ExpressionAttributeValues={":new": {"N": str(new.token)}, ":json": {"S": new.model_dump_json()},
                                           ":exp": {"N": str(_ms(new.expires_at))},
                                           ":old": {"N": str(expected.token)}, ":false": {"BOOL": False},
                                           ":oldjson": {"S": expected.model_dump_json()},
                                           ":now": {"N": str(_ms(new.acquired_at))}},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise LeaseConflictError("lease changed since it was read") from None
            raise

    def append_execution_event(self, event, lease_token, lease_expires_at, valid_after=None) -> None:
        lease = self.get_lease(event.proposal_id, event.execution_id)
        if lease is None:
            raise LeaseConflictError("lease missing")
        renewed = lease.model_copy(update={"expires_at": lease_expires_at})
        try:
            self.client.transact_write_items(TransactItems=[
                self._fenced_lease_update(event.proposal_id, event.execution_id, lease_token, renewed,
                                          valid_after or event.at),
                {"Put": {"TableName": self.table, "Item": self._exec_event_item(event),
                         "ConditionExpression": "attribute_not_exists(PK)"}},
            ])
        except ClientError as exc:
            if not _cancelled(exc):
                raise
            c = _cancellation(exc, ("lease", "event"))
            if c is not None and c.other:
                raise
            if c is not None and not c.failed and c.transient:
                raise _transient(c, "the execution event") from None
            if c is not None and c.failed == {"event"}:
                raise LeaseConflictError(f"execution event {event.seq} already exists") from None
            raise LeaseConflictError(f"execution {event.execution_id} lease token {lease_token} is no longer current "
                                     "(taken over, released or expired)") from None

    def list_execution_events(self, proposal_id, execution_id):
        out = []
        for page in self.client.get_paginator("query").paginate(
                TableName=self.table, ConsistentRead=True, KeyConditionExpression="PK = :pk AND begins_with(SK, :x)",
                ExpressionAttributeValues={":pk": {"S": _pk(proposal_id)},
                                           ":x": {"S": self._exec_sk(execution_id, "EVT#")}}):
            out += [self._load(i, lambda x: ExecutionEvent.model_validate_json(x["event_json"]["S"]))
                    for i in page.get("Items", [])]
        return out

    def finish_execution(self, event, expected, new_head, final, lease_token) -> None:
        lease = self.get_lease(final.proposal_id, final.execution_id)
        if lease is None:
            raise LeaseConflictError("lease missing")
        not_exists = "attribute_not_exists(PK)"
        try:
            self.client.transact_write_items(TransactItems=[
                self._head_update(event, expected, new_head),
                {"Put": {"TableName": self.table, "Item": self._event_item(event), "ConditionExpression": not_exists}},
                self._fenced_lease_update(final.proposal_id, final.execution_id, lease_token,
                                          lease.model_copy(update={"released": True}), final.at),
                {"Put": {"TableName": self.table, "Item": self._exec_event_item(final), "ConditionExpression": not_exists}},
            ])
        except ClientError as exc:
            if not _cancelled(exc):
                raise
            c = _cancellation(exc, ("head", "event", "lease", "final"))
            if c is not None and c.other:
                raise
            if c is not None and not c.failed and c.transient:
                raise _transient(c, "the execution finalisation") from None
            if c is not None and "lease" not in c.failed:
                raise LeaseConflictError("execution could not be finalized: the proposal head or events changed") from None
            raise LeaseConflictError("execution could not be finalized (lease or head changed)") from None

    def record_abort(self, event, expected, new_head, decision, nonce, idempotency, abort_event, expected_lease,
                     new_lease) -> None:
        """Batch 2 (M1): one transaction records the ABORT decision, moves the head EXECUTING -> ABORTED,
        revokes the lease (new token, released) and closes the execution's audit chain."""
        _check_decision_id(decision.decision_id)
        not_exists = "attribute_not_exists(PK)"
        nonce_key = {"PK": {"S": f"NONCE#{nonce.nonce}"}, "SK": {"S": "USED"}}
        try:
            self.client.transact_write_items(TransactItems=[
                self._head_update(event, expected, new_head),
                {"Put": {"TableName": self.table, "Item": self._event_item(event), "ConditionExpression": not_exists}},
                {"Put": {"TableName": self.table, "ConditionExpression": not_exists, "Item": {
                    "PK": {"S": _pk(decision.proposal_id)}, "SK": {"S": f"DECISION#{decision.decision_id}"},
                    "entity": {"S": "DecisionRecord"}, "decision": {"S": decision.decision.value},
                    "decision_json": {"S": decision.model_dump_json()}}}},
                {"Put": {"TableName": self.table, "ConditionExpression": not_exists, "Item": {
                    **nonce_key, "entity": {"S": "NonceRecord"}, "record_json": {"S": nonce.model_dump_json()},
                    "ttl_epoch": {"N": str(nonce.ttl_epoch)}}}},
                {"Put": {"TableName": self.table, "Item": self._idem_item(idempotency), "ConditionExpression": not_exists}},
                {"Put": {"TableName": self.table, "Item": self._lease_item(new_lease),
                         "ConditionExpression": "#t = :old AND released = :false AND lease_json = :oldjson",
                         "ExpressionAttributeNames": {"#t": "token"},
                         "ExpressionAttributeValues": {":old": {"N": str(expected_lease.token)}, ":false": {"BOOL": False},
                                                       ":oldjson": {"S": expected_lease.model_dump_json()}}}},
                {"Put": {"TableName": self.table, "Item": self._exec_event_item(abort_event),
                         "ConditionExpression": not_exists}},
            ])
        except ClientError as exc:
            if not _cancelled(exc):
                raise
            c = _cancellation(exc, ("head", "event", "decision", "nonce", "idempotency", "lease", "abort_event"))
            if c is not None and not c.other:
                if "idempotency" in c.failed:
                    existing = self.get_idempotency(idempotency.scope, idempotency.key)
                    if existing is not None:
                        raise IdempotencyKeyTakenError(existing) from None
                if "nonce" in c.failed:
                    raise NonceReplayError("decision nonce was already used") from None
                if c.failed:
                    raise TransitionConflictError(f"execution of {event.proposal_id} changed since it was read") from None
                if c.transient:
                    raise _transient(c, "the abort") from None
            elif c is not None:
                raise
            if self._get(nonce_key) is not None:
                raise NonceReplayError("decision nonce was already used") from None
            existing = self.get_idempotency(idempotency.scope, idempotency.key)
            if existing is not None:
                raise IdempotencyKeyTakenError(existing) from None
            raise TransitionConflictError(f"execution of {event.proposal_id} changed since it was read") from None

    def repair_head(self, head, expected_seq) -> None:
        if head.seq <= expected_seq:
            return
        with contextlib.suppress(ClientError):
            self.client.put_item(
                TableName=self.table, Item=self._head_item(head),
                ConditionExpression="#seq = :expected", ExpressionAttributeNames={"#seq": "seq"},
                ExpressionAttributeValues={":expected": {"N": str(expected_seq)}},
            )


# ===================================================================== consistent multi-record reads (M4)

MAX_READ_ATTEMPTS = 5


def read_proposal_state(repo: ProposalRepository, proposal_id: str, attempts: int = MAX_READ_ATTEMPTS):
    """(stored, head, events, decisions) from ONE committed state.

    Local stores read inside one read transaction (a snapshot), so the first attempt is always
    consistent. DynamoDB has no multi-item snapshot: the head is read before and after the records,
    and every commit that adds an event or decision also advances the head's seq, so an unchanged
    head means the records belong to that head. A moved head is retried; integrity checks then run
    on a consistent set, so a concurrent writer is never reported as tampering."""
    for _ in range(attempts):
        with repo.read_snapshot():
            before = repo.get_head(proposal_id)
            stored = repo.get_stored(proposal_id)
            events = repo.list_events(proposal_id)
            decisions = repo.list_decisions(proposal_id)
            after = repo.get_head(proposal_id)
        if before == after:
            return stored, after, events, decisions
    raise ConsistentReadError(f"proposal {proposal_id} kept changing while it was read; retry")


def read_execution_state(repo: ProposalRepository, proposal_id: str, execution_id: str,
                         attempts: int = MAX_READ_ATTEMPTS):
    """(stored, lifecycle events, record, lease, execution events) from ONE committed state.
    Finalisation, abort and lease takeover change the head and/or the lease, so an unchanged
    (head, lease) pair around the reads means the execution events form a committed prefix that
    matches that lease; otherwise retry (DynamoDB). Local stores use one read transaction."""
    for _ in range(attempts):
        with repo.read_snapshot():
            head1, lease1 = repo.get_head(proposal_id), repo.get_lease(proposal_id, execution_id)
            stored = repo.get_stored(proposal_id)
            record = repo.get_execution(proposal_id, execution_id)
            lifecycle = repo.list_events(proposal_id)
            events = repo.list_execution_events(proposal_id, execution_id)
            head2, lease2 = repo.get_head(proposal_id), repo.get_lease(proposal_id, execution_id)
        if (head1, lease1) == (head2, lease2):
            return stored, lifecycle, record, lease2, events
    raise ConsistentReadError(f"execution {execution_id} kept changing while it was read; retry")


# ===================================================================== manager


class ProposalManager:
    """Business rules over any ProposalRepository: store only approvable proposals, verify every
    read, expire lazily and exactly once, and make creation idempotent."""

    MAX_EXPIRY_ATTEMPTS = 5

    def __init__(self, repository: ProposalRepository, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 policy_manager: PolicyManager | None = None):
        self.repo = repository
        self.clock = clock
        self.policy_manager = policy_manager

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset().total_seconds() != 0:
            raise ValueError("clock must return timezone-aware UTC")
        return now

    # ---- create

    def save(self, proposal: TaggingProposal, *, idempotency_key: str, requested_by: Actor,
             resource_identities: dict[str, str] | None = None,
             requester_identity: IdentityEvidence | None = None) -> CreateResult:
        proposal = TaggingProposal.model_validate(proposal.model_dump(mode="json"))  # never trust the object
        now = self._now()
        if not proposal.is_approvable:
            raise ProposalNotApprovableError(f"proposal is {proposal.approval_status.value}: {'; '.join(proposal.blocking_reasons)}")
        if not proposal.approvable_at(now):
            raise ProposalNotApprovableError("proposal has already expired")
        if self.policy_manager is not None:
            self.policy_manager.resolve(proposal.policy)  # policy version must exist with this exact hash

        stored = StoredProposal.from_proposal(proposal, created_by=requested_by, idempotency_key=idempotency_key,
                                              stored_at=now, resource_identities=resource_identities,
                                              requester_identity=requester_identity)
        if len(stored.model_dump_json().encode()) > MAX_PROPOSAL_BYTES:
            raise ProposalTooLargeError(f"proposal exceeds {MAX_PROPOSAL_BYTES} bytes; narrow the request")
        record = IdempotencyRecord(
            scope=IDEMPOTENCY_SCOPE, key=idempotency_key, request_fingerprint=stored.request_fingerprint,
            proposal_id=proposal.proposal_id, created_at=now,
            ttl_epoch=int((now + IDEMPOTENCY_RETENTION).timestamp()),
        )
        existing = self.repo.get_idempotency(IDEMPOTENCY_SCOPE, idempotency_key)
        if existing is not None:
            return self._replay(existing, stored.request_fingerprint)

        event = LifecycleEvent(
            proposal_id=proposal.proposal_id, seq=1, event_type=event_type_for(None, ProposalState.PENDING),
            from_state=None, to_state=ProposalState.PENDING, actor=requested_by, at=now,
            reason="approvable proposal stored", content_hash=proposal.content_hash,
            details={"operation": proposal.operation.value, "resources_changing": proposal.impact.resources_changing,
                     "requester_authenticated": requested_by.authenticated,
                     "requester_person": requester_identity.person_id if requester_identity else None},
        )
        head = ProposalHead(proposal_id=proposal.proposal_id, state=ProposalState.PENDING, seq=1,
                            content_hash=proposal.content_hash, proposal_expires_at=proposal.expires_at, updated_at=now)
        try:
            self.repo.create(stored, head, event, record)
        except IdempotencyKeyTakenError as taken:  # lost a race with an identical key
            return self._replay(taken.record, stored.request_fingerprint)
        return CreateResult(proposal_id=proposal.proposal_id, created=True, head=head)

    def _replay(self, record: IdempotencyRecord, fingerprint: str) -> CreateResult:
        if record.request_fingerprint != fingerprint:
            raise IdempotencyConflictError("idempotency key was already used for a different request")
        view = self.get(record.proposal_id)
        return CreateResult(proposal_id=record.proposal_id, created=False, head=view.head)

    # ---- read (verifies everything, then expires lazily)

    def get(self, proposal_id: str) -> ProposalView:
        return self._get(proposal_id)[0]

    def _get(self, proposal_id: str) -> tuple[ProposalView, list[LifecycleEvent]]:
        """The verified view plus the exact event list it was verified against (M4: one committed
        state per attempt; see read_proposal_state)."""
        _check_id(proposal_id)
        for _ in range(self.MAX_EXPIRY_ATTEMPTS):
            stored, head, events, decisions = read_proposal_state(self.repo, proposal_id)
            if stored is None:
                raise ProposalNotFoundError(f"proposal {proposal_id} not found")
            if stored.proposal_id != proposal_id:
                raise ProposalIntegrityError(f"record for {proposal_id} contains {stored.proposal_id}")
            proposal = stored.load()
            if self.policy_manager is not None:
                self.policy_manager.resolve(stored.policy)
            if head is None:
                raise ProposalIntegrityError(f"proposal {proposal_id} has no state head")
            derived = verify_head(head, stored, events)
            verify_decisions(stored, events, decisions)
            if derived.seq > head.seq:  # head fell behind (crash between event and head write)
                self.repo.repair_head(derived, head.seq)
                head = derived
            deadline = head.expiry_deadline()
            if head.state in EXPIRABLE_STATES and deadline is not None and self._now() >= deadline:
                try:
                    self._expire(head)
                except TransitionConflictError:
                    pass  # someone else changed the state first; re-read and re-evaluate
                continue
            return ProposalView(proposal=proposal, head=head, created_by=stored.created_by), events
        raise TransitionConflictError(f"proposal {proposal_id} kept changing while it was read")

    def _expire(self, head: ProposalHead) -> None:
        now = self._now()
        event = LifecycleEvent(
            proposal_id=head.proposal_id, seq=head.seq + 1,
            event_type=event_type_for(head.state, ProposalState.EXPIRED),
            from_state=head.state, to_state=ProposalState.EXPIRED, actor=SYSTEM_EXPIRY_ACTOR, at=now,
            reason=("approval expiry reached" if head.state == ProposalState.APPROVED
                    and head.expiry_deadline() != head.proposal_expires_at else "proposal expiry reached"),
            content_hash=head.content_hash,
            details={"proposal_expires_at": head.proposal_expires_at.isoformat(), "observed_at": now.isoformat(),
                     "approval_expires_at": head.approval_expires_at.isoformat() if head.approval_expires_at else None},
        )
        new_head = head.model_copy(update={"state": ProposalState.EXPIRED, "seq": head.seq + 1, "updated_at": now})
        self.repo.append_transition(event, head, new_head)

    def head(self, proposal_id: str) -> ProposalHead:
        return self.get(proposal_id).head

    def history(self, proposal_id: str) -> list[LifecycleEvent]:
        return self._get(proposal_id)[1]  # exactly the events that were verified (after lazy expiry)

    def is_approvable_now(self, proposal_id: str) -> bool:
        """Whether a future approval could even be considered. Never true after expiry."""
        view = self.get(proposal_id)
        return view.head.state == ProposalState.PENDING and view.proposal.approvable_at(self._now())


def new_idempotency_key() -> str:
    return f"auto-{uuid.uuid4()}"


__all__ = [
    "DuplicateProposalError", "DynamoProposalRepository", "IdempotencyConflictError", "LocalProposalRepository",
    "ProposalManager", "ProposalNotApprovableError", "ProposalNotFoundError", "ProposalRepository",
    "ProposalTooLargeError", "TransitionConflictError", "new_idempotency_key", "request_fingerprint",
]
