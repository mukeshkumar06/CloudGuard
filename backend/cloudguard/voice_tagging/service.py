"""Voice tagging: prepare a tagging change, bind a spoken confirmation to it, and execute it with the existing
proposal, approval and execution workflow.

Nothing here writes tags itself. The flow for one operation:

prepare (read-only)
  1. The operator session is valid, the feature is enabled and the kill switch is off.
  2. The AWS caller identity comes from STS (live) and its account must be in CLOUDGUARD_VOICE_TAGGING_ACCOUNTS.
     Nothing from the browser names an account.
  3. CustomTaggingEngine resolves the request (service, region, name filter or exact name, tags) against fresh
     discovery, exactly as `proposal save` does.
  4. A confirmation challenge is created: a random id, bound to the session, expiring after CHALLENGE_TTL, and
     carrying the plan hash (the account, region, request and every resource's current tags and changes).

confirm (the only path to a write)
  5. The challenge must belong to this session, be pending and unexpired; the server reads the transcript itself
     (confirm.py: an exact affirmative phrase only) and, when values would be overwritten, a separate overwrite
     acknowledgement.
  6. The challenge moves to EXECUTING under a lock before anything else, so a repeated transcript or a replayed
     request can never execute twice (a finished challenge returns its stored result).
  7. Identity and account are verified again; the request is resolved again from AWS and its plan hash must
     equal the confirmed one, else nothing is written.
  8. DecisionService (with a PersonalAccountWaiver for this one account) creates and approves the proposal, and
     the existing executor runs it: revalidation, a re-read before every write, a fenced write intent,
     read-after-write verification, one result per resource. The kill switch is checked before every write.
  9. The result is recorded in the audit log and reported per resource.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from cloudguard.governance.custom_tagging import CustomTaggingEngine
from cloudguard.governance.custom_tagging.models import (
    NameFilter, OutcomeStatus, TaggingOperation, TaggingRequest, TargetCriteria,
)
from cloudguard.governance.custom_tagging.validation import validate_tag
from cloudguard.governance.policy_store import PolicyManager
from cloudguard.governance.remediation.approval import (
    AuthorizationError, DecisionNotAllowedError, DecisionService, PersonalAccountWaiver, new_nonce,
)
from cloudguard.governance.remediation.authz import AuthzManager
from cloudguard.governance.remediation.execution_models import ResultStatus
from cloudguard.governance.remediation.executor import (
    ConcurrentExecutionError, ExecutionNotAllowedError, FixtureRemediationExecutor, LiveRemediationExecutor,
)
from cloudguard.governance.remediation.identity import IdentityError
from cloudguard.governance.remediation.models import OperatingMode, ProposalIntegrityError, canonical_digest
from cloudguard.governance.remediation.repository import IdempotencyConflictError, ProposalManager
from cloudguard.voice_tagging.confirm import is_overwrite_ack, read_confirmation
from cloudguard.voice_tagging.config import VoiceTaggingConfig

CHALLENGE_TTL = timedelta(minutes=2)
MAX_RESOURCES = 50  # one spoken confirmation covers at most this many resources
RESOURCE_TYPES = {"lambda": "AWS::Lambda::Function", "dynamodb": "AWS::DynamoDB::Table", "s3": "AWS::S3::Bucket",
                  "ec2": "AWS::EC2::Instance"}  # exactly the types live_writer supports
SERVICE_NOUN = {"lambda": "Lambda function", "dynamodb": "DynamoDB table", "s3": "S3 bucket", "ec2": "EC2 instance"}
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")
_ACCOUNT_IN_TEXT = re.compile(r"\b\d{12}\b")
_ARN_IN_TEXT = re.compile(r"arn:aws[a-z-]*:[^\s,;]+")


class VoiceTaggingError(Exception):
    """A refused request. `code` is stable for the client; `message` is safe to show and speak."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _safe(text: str) -> str:
    """No ARNs or account IDs in messages that leave the server."""
    return _ACCOUNT_IN_TEXT.sub("<account>", _ARN_IN_TEXT.sub("<arn>", text))[:300]


def account_ref(account_id: str) -> str:
    return f"…{account_id[-4:]}"


class TagSpec(BaseModel):
    """What the operator asked for. Parsed by the browser, validated here; never trusted for the account."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: Literal["lambda", "dynamodb", "s3", "ec2"]
    region: str = Field(pattern=_REGION.pattern)
    match: Literal["contains", "starts_with", "ends_with", "exact"]
    text: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")
    tags: dict[str, str] = Field(min_length=1, max_length=10)
    overwrite: bool = False

    def request(self) -> TaggingRequest:
        rtype = RESOURCE_TYPES[self.service]
        if self.match == "exact":  # one resource by exact name; duplicates come back as a clarification
            target = TargetCriteria(resource_type=rtype, region=self.region, name=self.text)
            op = TaggingOperation.UPDATE_TAG if self.overwrite else TaggingOperation.ADD_TAG
        else:
            target = TargetCriteria(resource_type=rtype, region=self.region, all_of_type=True,
                                    name_filter=NameFilter(match=self.match, text=self.text))
            op = TaggingOperation.BULK_UPDATE_TAG if self.overwrite else TaggingOperation.BULK_ADD_TAG
        return TaggingRequest(operation=op, target=target, tags=dict(self.tags), raw_text="voice tagging",
                              parsed_by="voice")


class Backend(Protocol):
    """Where identity, discovery and writes come from: the live AWS account, or the simulated fixture account."""

    mode: OperatingMode
    label: str

    def identity(self): ...  # an IdentityProvider (STS in live mode)

    def source(self, region: str, service: str): ...  # a DiscoverySource for one region and service

    def tag_account(self, account_id: str): ...  # LiveTagAccount (assumed tagging role) or FixtureAccount

    def execution_identity(self) -> str: ...  # the writer's identity, for the audit (account masked)


@dataclass
class Challenge:
    id: str
    session: str  # the operator session's id hash
    spec: TagSpec
    account_id: str
    plan_hash: str
    created_at: datetime
    expires_at: datetime
    requires_overwrite_ack: bool
    names: dict[str, str]  # arn -> resource name
    already: list[str]  # names already carrying the requested tags (no write needed)
    state: Literal["PENDING", "EXECUTING", "DONE", "CANCELLED", "EXPIRED", "INVALIDATED"] = "PENDING"
    result: dict | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def plan_hash(proposal, spec: TagSpec) -> str:
    """Binds a confirmation to exactly this operation: account, region, request and every resource with its
    current tags and the changes it would get. Any difference means the user didn't confirm it."""
    records = sorted(
        ({"arn": r.resource_arn, "outcome": r.outcome.value, "current": r.current_tags,
          "changes": [c.model_dump(mode="json", include={"key", "action", "old_value", "new_value"}) for c in r.changes]}
         for r in proposal.records if r.outcome.value != "OUT_OF_SCOPE"), key=lambda x: x["arn"])
    return canonical_digest({"account": proposal.account_id, "spec": spec.model_dump(mode="json"),
                             "operation": proposal.operation.value, "records": records})


class AuditLog:
    """Append-only JSON lines. Never credentials, tokens or session ids; the account only as a masked reference."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")


_CATEGORY = {
    ResultStatus.APPLIED_VERIFIED: "tagged", ResultStatus.ALREADY_APPLIED: "already_correct",
    ResultStatus.SKIPPED: "skipped", ResultStatus.NOT_ATTEMPTED: "skipped",
    ResultStatus.FAILED_WRITE: "failed", ResultStatus.FAILED_VERIFICATION: "failed",
    ResultStatus.UNCONFIRMED_WRITE: "failed",
}


def _names(names: list[str], limit: int = 10) -> str:
    shown = names[:limit]
    rest = f" and {len(names) - limit} more" if len(names) > limit else ""
    return (", ".join(shown[:-1]) + " and " + shown[-1] if len(shown) > 1 else shown[0]) + rest if shown else ""


class VoiceTaggingService:
    def __init__(self, config: VoiceTaggingConfig, backend: Backend, proposals: ProposalManager,
                 authz: AuthzManager, policies: PolicyManager, audit: AuditLog,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.config, self.backend, self.proposals, self.authz, self.policies = config, backend, proposals, authz, policies
        self.audit, self.clock = audit, clock
        self._challenges: dict[str, Challenge] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ switches

    def killed(self) -> str | None:
        """The kill switch: a reason while it is engaged (checked on every request and before every write)."""
        if not self.config.enabled:
            return "voice tagging is disabled"
        if self.config.kill_file.exists():
            return "voice tagging kill switch is engaged"
        return None

    def engage_kill_switch(self) -> None:
        self.config.kill_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.kill_file.write_text("voice tagging disabled by the operator\n", encoding="utf-8")
        with self._lock:
            for c in self._challenges.values():
                if c.state == "PENDING":
                    c.state = "CANCELLED"

    def _require_on(self) -> None:
        if (reason := self.killed()) is not None:
            raise VoiceTaggingError("DISABLED", reason[0].upper() + reason[1:] + ". Nothing was written.", 403)

    def _caller_account(self) -> tuple[object, str]:
        """The STS-verified caller (live) and its account, which must be allow-listed."""
        provider = self.backend.identity()
        try:
            principal = provider.current()
        except IdentityError:
            raise VoiceTaggingError("IDENTITY_UNVERIFIED", "The AWS caller identity could not be verified. "
                                    "Nothing was written.", 403) from None
        except Exception:  # noqa: BLE001 - STS unreachable, expired credentials, ...
            raise VoiceTaggingError("IDENTITY_UNVERIFIED", "The AWS caller identity could not be verified. "
                                    "Nothing was written.", 403) from None
        account = principal.account_id if self.backend.mode == OperatingMode.LIVE else getattr(
            self.backend, "account_id", None)
        if not account or account not in self.config.accounts:
            raise VoiceTaggingError("ACCOUNT_NOT_ALLOWED", "The AWS account in use is not on the voice tagging "
                                    "allow-list. Nothing was written.", 403)
        return provider, account

    # ------------------------------------------------------------------ prepare

    def prepare(self, session: str, spec: TagSpec) -> dict:
        self._require_on()
        self._expire()
        for key, value in spec.tags.items():
            if (bad := validate_tag(key, value)) is not None:
                raise VoiceTaggingError("INVALID_TAG", f"{bad.message}. Nothing was written.", 400)
            if key.lower().startswith("cloudguard:"):
                raise VoiceTaggingError("INVALID_TAG", "Keys starting with cloudguard: are reserved. Nothing was "
                                        "written.", 400)
        if spec.overwrite and len(spec.tags) != 1:
            raise VoiceTaggingError("INVALID_TAG", "An overwrite changes exactly one tag.", 400)
        _, account = self._caller_account()
        outcome = self._resolve(spec)
        proposal = outcome.proposal
        base = {"mode": self.backend.label, "account_ref": account_ref(account), "service": spec.service,
                "region": spec.region, "tags": dict(spec.tags), "overwrite": spec.overwrite}
        if proposal is not None and proposal.account_id != account:
            raise VoiceTaggingError("ACCOUNT_MISMATCH", "Discovery answered for a different account than the "
                                    "verified caller. Nothing was written.", 403)
        records = [r for r in (proposal.records if proposal else []) if r.outcome.value != "OUT_OF_SCOPE"]
        if len(records) > MAX_RESOURCES:
            raise VoiceTaggingError("TOO_MANY", f"That matches {len(records)} resources; one voice confirmation "
                                    f"covers at most {MAX_RESOURCES}. Narrow the name filter.", 400)
        rows = [{"name": r.resource_name, "region": r.region, "outcome": r.outcome.value,
                 "current_value": {k: (r.current_tags or {}).get(k) for k in spec.tags},
                 "changes": [{"key": c.key, "action": c.action.value, "old_value": c.old_value,
                              "new_value": c.new_value} for c in r.changes],
                 "conflicts": [{"key": c.key, "existing_value": c.existing_value, "reason": c.reason.value}
                               for c in r.conflicts]} for r in records]
        candidates = [c.resource_name for c in outcome.candidates][:20]
        if outcome.status != OutcomeStatus.PROPOSAL_READY:
            return base | {"status": outcome.status.value, "resources": rows, "candidates": candidates,
                           "challenge": None, "speech": self._not_ready_speech(outcome.status, rows, spec)}
        overwrites = [{"name": r["name"], **c} for r in rows for c in r["changes"] if c["action"] == "OVERWRITE"]
        challenge = Challenge(
            id=secrets.token_urlsafe(24), session=session, spec=spec, account_id=account,
            plan_hash=plan_hash(proposal, spec), created_at=self.clock(), expires_at=self.clock() + CHALLENGE_TTL,
            requires_overwrite_ack=proposal.requires_overwrite_confirmation,
            names={r.resource_arn: r.resource_name for r in records},
            already=[r.resource_name for r in records if r.outcome.value == "ALREADY_MATCHING"])
        with self._lock:
            for c in self._challenges.values():  # a new operation replaces every earlier pending confirmation
                if c.session == session and c.state == "PENDING":
                    c.state = "CANCELLED"
            self._challenges[challenge.id] = challenge
        return base | {"status": "READY", "resources": rows, "candidates": [], "overwrites": overwrites,
                       "requires_overwrite_ack": challenge.requires_overwrite_ack,
                       "challenge": {"id": challenge.id, "expires_at": challenge.expires_at.isoformat(),
                                     "plan_hash": challenge.plan_hash},
                       "speech": self._ready_speech(spec, rows, overwrites)}

    def _resolve(self, spec: TagSpec):
        engine = CustomTaggingEngine(self.backend.source(spec.region, spec.service), self.policies.get_active(),
                                     clock=self.clock)
        try:
            return engine.prepare_request(spec.request())
        except ValueError as exc:
            raise VoiceTaggingError("INVALID_REQUEST", _safe(f"Invalid tagging request: {exc}"), 400) from None

    @staticmethod
    def _ready_speech(spec: TagSpec, rows: list[dict], overwrites: list[dict]) -> str:
        changing = [r["name"] for r in rows if r["changes"]]
        noun = SERVICE_NOUN[spec.service] + ("s" if len(changing) != 1 else "")
        tags = ", ".join(f"{k} equals {v}" for k, v in spec.tags.items())
        text = f"{len(changing)} {noun} would get {tags}: {_names(changing)}."
        already = [r["name"] for r in rows if r["outcome"] == "ALREADY_MATCHING"]
        if already:
            text += f" Already correct, no change: {_names(already)}."
        if overwrites:
            text += " This overwrites existing values: " + "; ".join(
                f"{o['name']}, {o['key']} from {o['old_value']} to {o['new_value']}" for o in overwrites) + "."
        return text

    @staticmethod
    def _not_ready_speech(status, rows, spec) -> str:
        conflicts = [r for r in rows if r["conflicts"]]
        if status == OutcomeStatus.NO_CHANGES and conflicts:
            return (f"No tag would change. {len(conflicts)} resource{'s' if len(conflicts) != 1 else ''} already "
                    f"{'have' if len(conflicts) != 1 else 'has'} a different value: " + "; ".join(
                        f"{r['name']} has {c['key']} equals {c['existing_value']}" for r in conflicts
                        for c in r["conflicts"]) + ". Adding a tag never overwrites. Nothing was written.")
        if status == OutcomeStatus.NO_CHANGES:
            return "Every matching resource already has that tag and value. Nothing needs to change."
        if status == OutcomeStatus.NOT_FOUND:
            return f"I found no {SERVICE_NOUN[spec.service]} matching that in {spec.region}. Nothing was written."
        if status == OutcomeStatus.NEEDS_CLARIFICATION:
            return "More than one resource fits that name. Say the exact name. Nothing was written."
        if status == OutcomeStatus.BLOCKED:
            return "The tagging policy doesn't allow that value. Nothing was written."
        return "I can't prepare that change. Nothing was written."

    # ------------------------------------------------------------------ confirm / cancel

    def _get(self, session: str, challenge_id: str) -> Challenge:
        with self._lock:
            c = self._challenges.get(challenge_id) if isinstance(challenge_id, str) else None
        if c is None or not secrets.compare_digest(c.session, session):
            raise VoiceTaggingError("NO_CHALLENGE", "There is no pending confirmation for this session. Nothing "
                                    "was written.", 404)
        return c

    def cancel(self, session: str, challenge_id: str) -> dict:
        c = self._get(session, challenge_id)
        with c.lock:
            if c.state == "PENDING":
                c.state = "CANCELLED"
        return {"status": c.state}

    def end_session(self, session: str) -> None:
        """Logout, expiry or disconnect: every pending confirmation of the session is void."""
        with self._lock:
            for c in self._challenges.values():
                if c.session == session and c.state == "PENDING":
                    c.state = "CANCELLED"

    def _expire(self) -> None:
        now = self.clock()
        with self._lock:
            for key, c in list(self._challenges.items()):
                if c.state == "PENDING" and now >= c.expires_at:
                    c.state = "EXPIRED"
                if now - c.created_at > timedelta(hours=1):
                    del self._challenges[key]  # bounded memory; results are in the audit log

    def confirm(self, session: str, operator: str, challenge_id: str, transcript: str,
                overwrite_transcript: str | None = None) -> dict:
        c = self._get(session, challenge_id)
        if not c.lock.acquire(blocking=False):
            raise VoiceTaggingError("IN_PROGRESS", "That operation is already running.", 409)
        try:
            if c.state == "DONE":
                return c.result | {"replayed": True}  # a repeated "yes" never executes twice
            if c.state != "PENDING":
                raise VoiceTaggingError(f"CHALLENGE_{c.state}", {
                    "EXECUTING": "That operation is already running.",
                    "CANCELLED": "That confirmation was cancelled. Nothing was written.",
                    "EXPIRED": "That confirmation expired. Nothing was written.",
                    "INVALIDATED": "The resources changed after that confirmation. Nothing was written.",
                }[c.state], 409)
            if self.clock() >= c.expires_at:
                c.state = "EXPIRED"
                raise VoiceTaggingError("CHALLENGE_EXPIRED", "That confirmation expired. Nothing was written.", 409)
            heard = read_confirmation(transcript)
            if heard == "no":
                c.state = "CANCELLED"
                return {"status": "CANCELLED", "speech": "Cancelled. Nothing was written."}
            if heard != "yes":
                raise VoiceTaggingError("NOT_CONFIRMED", "That wasn't a clear yes. Nothing was written.", 400)
            if c.requires_overwrite_ack and not is_overwrite_ack(overwrite_transcript):
                raise VoiceTaggingError("OVERWRITE_NOT_ACKNOWLEDGED", "Overwriting existing values needs its own "
                                        "acknowledgement: say 'I confirm the overwrite'. Nothing was written.", 400)
            self._require_on()
            c.state = "EXECUTING"  # from here this challenge can never run again
            confirmed_at = self.clock()
            try:
                c.result = self._execute(c, operator, confirmed_at)
            except VoiceTaggingError as exc:
                c.state = "INVALIDATED" if exc.code == "PROPOSAL_CHANGED" else "DONE"
                c.result = {"status": "REFUSED", "code": exc.code, "speech": exc.message}
                self._audit(c, operator, confirmed_at, None, refused=exc.code)
                raise
            c.state = "DONE"
            return c.result
        finally:
            c.lock.release()

    def _execute(self, c: Challenge, operator: str, confirmed_at: datetime) -> dict:
        provider, account = self._caller_account()
        if account != c.account_id:
            raise VoiceTaggingError("ACCOUNT_MISMATCH", "The AWS account changed since the confirmation. Nothing "
                                    "was written.", 403)
        outcome = self._resolve(c.spec)  # resolved again from AWS: never trust what was confirmed earlier
        if (outcome.status != OutcomeStatus.PROPOSAL_READY or outcome.proposal is None
                or outcome.proposal.account_id != account or plan_hash(outcome.proposal, c.spec) != c.plan_hash):
            raise VoiceTaggingError("PROPOSAL_CHANGED", "The resources or their tags changed since you confirmed. "
                                    "Nothing was written. Ask again to hear the new plan.", 409)
        proposal = outcome.proposal
        try:
            tag_account = self.backend.tag_account(account)
        except VoiceTaggingError:
            raise
        except Exception:  # noqa: BLE001 - AssumeRole denied, role missing, ...
            raise VoiceTaggingError("EXECUTION_ROLE", "The tagging role could not be used. Nothing was "
                                    "written.", 403) from None
        if getattr(tag_account, "account_id", None) != account:
            raise VoiceTaggingError("ACCOUNT_MISMATCH", "The tagging role is in a different account. Nothing was "
                                    "written.", 403)
        waiver = PersonalAccountWaiver(account_id=account, mode=self.backend.mode)
        decisions = DecisionService(self.proposals, self.authz, provider, clock=self.clock, waiver=waiver)
        changing = [r.resource_arn for r in proposal.records if r.changes]
        executor_cls = LiveRemediationExecutor if self.backend.mode == OperatingMode.LIVE else FixtureRemediationExecutor
        try:
            created = decisions.create_proposal(proposal, idempotency_key=f"voice-create-{c.id}",
                                                resource_identities=tag_account.identities(changing))
            pid = created.proposal_id if hasattr(created, "proposal_id") else proposal.proposal_id
            approval = decisions.approve(pid, content_hash=proposal.content_hash, nonce=new_nonce(),
                                         idempotency_key=f"voice-approve-{c.id}",
                                         acknowledge_overwrites=c.requires_overwrite_ack,
                                         reason="spoken confirmation").decision
            view = executor_cls(decisions, tag_account, clock=self.clock, stop_check=self.killed).execute(
                pid, approval_id=approval.decision_id, idempotency_key=f"voice-exec-{c.id}")
        except (AuthorizationError, DecisionNotAllowedError, ExecutionNotAllowedError, IdentityError,
                IdempotencyConflictError, ConcurrentExecutionError, ProposalIntegrityError) as exc:
            raise VoiceTaggingError("NOT_AUTHORIZED", _safe(f"Not executed: {exc}"), 403) from None
        rows = []
        for arn, result in view.results.items():
            rows.append({"name": c.names.get(arn, "?"), "category": _CATEGORY[result.status],
                         "status": result.status.value, "error": result.error_category.value,
                         "revalidation": result.revalidation.value if result.revalidation else None,
                         "detail": _safe(result.detail)})
        rows += [{"name": n, "category": "already_correct", "status": "ALREADY_MATCHING", "error": "NONE",
                  "revalidation": None, "detail": "already had the tag before execution; nothing written"}
                 for n in c.already]
        executed_at = self.clock()
        result = {"status": "EXECUTED", "outcome": view.summary.outcome.value if view.summary else "UNKNOWN",
                  "mode": self.backend.label, "proposal_id": pid, "execution_id": view.record.execution_id,
                  "results": rows, "speech": self._result_speech(rows)}
        self._audit(c, operator, confirmed_at, result, executed_at=executed_at, proposal=proposal)
        return result

    @staticmethod
    def _result_speech(rows: list[dict]) -> str:
        by = {k: [r for r in rows if r["category"] == k] for k in ("tagged", "already_correct", "failed", "skipped")}
        parts = []
        if by["tagged"]:
            parts.append(f"Tagged and verified {len(by['tagged'])}: {_names([r['name'] for r in by['tagged']])}.")
        if by["already_correct"]:
            parts.append(f"Already correct {len(by['already_correct'])}: "
                         f"{_names([r['name'] for r in by['already_correct']])}.")
        if by["failed"]:
            parts.append(f"Failed {len(by['failed'])}: " + "; ".join(
                f"{r['name']} ({r['status'].lower().replace('_', ' ')}, {r['error'].lower().replace('_', ' ')})"
                for r in by["failed"]) + ".")
        if by["skipped"]:
            parts.append(f"Skipped {len(by['skipped'])}, nothing written: " + "; ".join(
                f"{r['name']} ({(r['revalidation'] or r['status']).lower().replace('_', ' ')})"
                for r in by["skipped"]) + ".")
        return " ".join(parts) or "Nothing was changed."

    def _audit(self, c: Challenge, operator: str, confirmed_at: datetime, result: dict | None, *,
               executed_at: datetime | None = None, proposal=None, refused: str | None = None) -> None:
        self.audit.append({
            "event": "voice_tagging_execution" if refused is None else "voice_tagging_refused",
            "operator": operator, "account_ref": account_ref(c.account_id), "mode": self.backend.label,
            "execution_identity": _ACCOUNT_IN_TEXT.sub("<account>", self.backend.execution_identity()), "service": c.spec.service,
            "region": c.spec.region, "filter": {"match": c.spec.match, "text": c.spec.text},
            "resources": sorted(c.names.values()), "tags": dict(c.spec.tags), "overwrite": c.spec.overwrite,
            "plan_hash": c.plan_hash, "proposal_hash": proposal.content_hash if proposal else None,
            "challenge_created_at": c.created_at.isoformat(), "confirmed_at": confirmed_at.isoformat(),
            "executed_at": executed_at.isoformat() if executed_at else None, "refused": refused,
            "results": [{k: r[k] for k in ("name", "category", "status", "error")} for r in (result or {}).get(
                "results", [])],
        })
