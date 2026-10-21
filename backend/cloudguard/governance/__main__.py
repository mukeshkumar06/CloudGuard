"""CLI for resource governance.

    python -m cloudguard.governance scan   [--mode fixture|live] [--region R ...] [--policy-version N]
    python -m cloudguard.governance tag "REQUEST" [--mode fixture|live]   (read-only proposal)
    python -m cloudguard.governance policy validate FILE
    python -m cloudguard.governance policy init|show|history
    python -m cloudguard.governance policy save FILE --expected-version N --author NAME [--note TEXT]
    python -m cloudguard.governance policy restore VERSION --expected-version N --author NAME
    python -m cloudguard.governance proposal save "REQUEST" [--idempotency-key KEY]
    python -m cloudguard.governance proposal show|history PROPOSAL_ID
    python -m cloudguard.governance proposal approve PROPOSAL_ID --content-hash H [--acknowledge-overwrites]
    python -m cloudguard.governance proposal reject|revoke PROPOSAL_ID --content-hash H --reason TEXT
    python -m cloudguard.governance proposal abort PROPOSAL_ID --content-hash H --execution-id E --reason TEXT
    python -m cloudguard.governance authz validate FILE | show | history
    python -m cloudguard.governance authz save FILE --expected-version N --mode fixture --dev-identity NAME | --mode live
    python -m cloudguard.governance proposal execute PROPOSAL_ID --approval-id A --mode fixture --dev-identity NAME
    python -m cloudguard.governance proposal execution-show PROPOSAL_ID EXECUTION_ID | execution-history PROPOSAL_ID

Identity never comes from a flag value: --mode live uses the caller's AWS credentials verified by
sts:GetCallerIdentity; --mode fixture (default) uses --dev-identity NAME, a development identity
that can never authorise live AWS operations. Decisions require an authorization allow-list saved
with `authz save`; without one, everything is denied. `authz save` is itself identity-bound: the
first save of a mode is a one-time bootstrap by the administrator named in
CLOUDGUARD_AUTHZ_BOOTSTRAP_ADMIN; every later change needs 'admin' in the active allow-list.

`proposal execute` is FIXTURE-ONLY: it changes tags in a simulated account (persisted in
--fixture-account, seeded from --fixture on first use) and never calls an AWS write API. There is
no live execution option.

Policies are stored locally under .cloudguard/policies by default, or in the CloudGuard DynamoDB
table with --table. --record saves raw live discovery as a fixture: review it before committing,
because it contains real resource names and tags.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import boto3

from cloudguard.governance.compliance import run_scan
from cloudguard.governance.custom_tagging import CustomTaggingEngine
from cloudguard.governance.custom_tagging.models import OutcomeStatus
from cloudguard.governance.remediation.approval import (
    AuthorizationError,
    DecisionNotAllowedError,
    DecisionService,
    new_nonce,
)
from cloudguard.governance.remediation.authz import (
    BOOTSTRAP_ENV,
    AuthzConflictError,
    AuthzDeniedError,
    AuthzIntegrityError,
    AuthzManager,
    AuthzNotFoundError,
    AuthzValidationError,
    DynamoAuthzStore,
    LocalAuthzStore,
    parse_authz,
)
from cloudguard.governance.remediation.identity import (
    DevelopmentIdentityProvider,
    IdentityError,
    StsCallerIdentityProvider,
)
from cloudguard.governance.remediation.audit import verify_execution
from cloudguard.governance.remediation.executor import (
    ConcurrentExecutionError,
    ExecutionNotAllowedError,
    FixtureRemediationExecutor,
)
from cloudguard.governance.remediation.models import ProposalIntegrityError, ProposalState
from cloudguard.governance.remediation.writers import FixtureAccount
from cloudguard.governance.remediation.repository import (
    DynamoProposalRepository,
    IdempotencyConflictError,
    NonceReplayError,
    LocalProposalRepository,
    ProposalManager,
    ProposalNotApprovableError,
    ProposalNotFoundError,
    ProposalTooLargeError,
    TransitionConflictError,
    new_idempotency_key,
    read_execution_state,
)
from cloudguard.governance.report import render_summary
from cloudguard.governance.discovery import FixtureSource, LiveSource
from cloudguard.governance.discovery.common import BOTO_CONFIG
from cloudguard.governance.models import DiscoveryResult
from cloudguard.governance.policy import (
    DEFAULT_POLICY_ID,
    PolicyIntegrityError,
    PolicyValidationError,
    parse_policy,
)
from cloudguard.governance.policy_store import (
    DynamoPolicyStore,
    LocalPolicyStore,
    PolicyConflictError,
    PolicyManager,
    PolicyNotFoundError,
)

DEFAULT_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "governance-demo.json"
DEFAULT_TAG_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "custom-tagging-demo.json"
DEFAULT_POLICY_DIR = Path(".cloudguard") / "policies"
DEFAULT_PROPOSAL_DIR = Path(".cloudguard") / "proposals"
DEFAULT_AUTHZ_DIR = Path(".cloudguard") / "authz"
DEFAULT_FIXTURE_ACCOUNT = Path(".cloudguard") / "fixture-account.json"
SIMULATED_BANNER = "FIXTURE EXECUTION: simulated account only - no AWS resource was or can be modified."


class _Recorded:
    """Live discovery that already ran, so --record doesn't call AWS twice."""

    mode = "live"

    def __init__(self, discovery: DiscoveryResult):
        self._discovery = discovery

    def discover(self) -> DiscoveryResult:
        return self._discovery


def _manager(args) -> PolicyManager:
    if args.table:
        session = boto3.Session(profile_name=args.profile)
        return PolicyManager(DynamoPolicyStore(session.client("dynamodb", config=BOTO_CONFIG), args.table))
    return PolicyManager(LocalPolicyStore(args.policy_store))


def _proposal_manager(args) -> ProposalManager:
    policies = _manager(args)
    if args.table:
        session = boto3.Session(profile_name=args.profile)
        repo = DynamoProposalRepository(session.client("dynamodb", config=BOTO_CONFIG), args.table)
    else:
        repo = LocalProposalRepository(args.proposal_store)
    return ProposalManager(repo, policy_manager=policies)


def _authz_manager(args, *, bootstrap: bool = False) -> AuthzManager:
    # The bootstrap administrator is read only for `authz save`, and only matters while a mode is
    # uninitialised; AuthzManager never consults it for an initialised mode.
    admin = os.environ.get(BOOTSTRAP_ENV) if bootstrap else None
    if args.table:
        session = boto3.Session(profile_name=args.profile)
        store = DynamoAuthzStore(session.client("dynamodb", config=BOTO_CONFIG), args.table)
    else:
        store = LocalAuthzStore(args.authz_store)
    return AuthzManager(store, bootstrap_admin=admin)


def _identity(args, parser):
    if args.mode == "live":
        if args.dev_identity:
            parser.error("--dev-identity is only valid with --mode fixture")
        session = boto3.Session(profile_name=args.profile)
        return StsCallerIdentityProvider(session.client("sts", config=BOTO_CONFIG))
    if not args.dev_identity:
        parser.error("--mode fixture requires --dev-identity NAME (a development identity, never authenticated)")
    return DevelopmentIdentityProvider(args.dev_identity)


def _decisions(args, parser) -> DecisionService:
    return DecisionService(_proposal_manager(args), _authz_manager(args), _identity(args, parser))


def _fixture_account(args) -> FixtureAccount:
    path = args.fixture_account
    if path.exists():
        return FixtureAccount.load(path)
    account = FixtureAccount.from_discovery(FixtureSource(args.fixture).discover())
    account.save(path)
    return account


def _print_execution(view) -> None:
    print(SIMULATED_BANNER, file=sys.stderr)
    revalidation = {}
    for e in view.events:
        if e.revalidation is not None:
            revalidation[e.resource_arn] = e.revalidation
    for arn in view.record.intended_resources:
        r = view.results.get(arn)
        rv = revalidation.get(arn)
        print(f"  {arn}: revalidation={rv.status.value if rv else '-'} "
              f"result={r.status.value if r else 'PENDING'}" + (f" ({r.detail})" if r and r.detail else ""),
              file=sys.stderr)
    if view.summary is not None:
        print(f"  outcome: {view.summary.outcome.value} ({view.summary.succeeded}/{view.summary.intended} succeeded)",
              file=sys.stderr)
    print(json.dumps({"simulated_account": True, "execution": view.model_dump(mode="json"),
                      "revalidation": {a: v.model_dump(mode="json") for a, v in revalidation.items()}}, indent=2))


def _cmd_proposal(args, parser) -> int:
    if args.proposal_cmd == "save":
        service = _decisions(args, parser)
        account = _fixture_account(args) if args.mode != "live" else None
        source = _live_source(args, parser) if args.mode == "live" else account
        outcome = CustomTaggingEngine(source, _policy(args)).prepare(args.request)
        if outcome.status != OutcomeStatus.PROPOSAL_READY:
            _print(outcome)
            print(f"error: not saved - only approvable proposals are stored (status {outcome.status.value})",
                  file=sys.stderr)
            return 1
        identities = None
        if account is not None:  # fixture: record immutable identities so recreation can be detected
            identities = account.identities([r.resource_arn for r in outcome.proposal.records if r.changes])
        _print(service.create_proposal(outcome.proposal, idempotency_key=args.idempotency_key or new_idempotency_key(),
                                       resource_identities=identities))
    elif args.proposal_cmd == "execute":
        account = _fixture_account(args)
        executor = FixtureRemediationExecutor(_decisions(args, parser), account)
        try:
            view = executor.execute(args.proposal_id, approval_id=args.approval_id,
                                    idempotency_key=args.idempotency_key or new_idempotency_key())
        finally:
            account.save(args.fixture_account)
        _print_execution(view)
        return {ProposalState.SUCCEEDED: 0, ProposalState.PARTIAL: 4}.get(view.summary.outcome, 5)
    elif args.proposal_cmd in ("execution-show", "execution-history"):
        manager = _proposal_manager(args)
        reader = _ExecutionReader(manager)
        if args.proposal_cmd == "execution-show":
            _print_execution(reader.load(args.proposal_id, args.execution_id))
        else:
            views = reader.history(args.proposal_id)
            print(SIMULATED_BANNER, file=sys.stderr)
            print(json.dumps([{"execution_id": v.record.execution_id, "started_at": v.record.started_at.isoformat(),
                               "outcome": v.summary.outcome.value if v.summary else "IN_PROGRESS",
                               "requested_by": v.record.requested_by.model_dump(), "approver": v.record.approver.model_dump(),
                               "lease_token": v.lease.token} for v in views], indent=2))
    elif args.proposal_cmd == "show":
        _print(_proposal_manager(args).get(args.proposal_id))
    elif args.proposal_cmd == "history":
        print(json.dumps([e.model_dump(mode="json") for e in _proposal_manager(args).history(args.proposal_id)], indent=2))
    else:
        service = _decisions(args, parser)
        common = dict(content_hash=args.content_hash, nonce=args.nonce or new_nonce(),
                      idempotency_key=args.idempotency_key or new_idempotency_key())
        if args.proposal_cmd == "approve":
            result = service.approve(args.proposal_id, acknowledge_overwrites=args.acknowledge_overwrites,
                                     reason=args.reason, **common)
        elif args.proposal_cmd == "reject":
            result = service.reject(args.proposal_id, reason=args.reason, **common)
        elif args.proposal_cmd == "abort":
            result = service.abort(args.proposal_id, execution_id=args.execution_id, reason=args.reason, **common)
        else:
            result = service.revoke(args.proposal_id, approval_id=args.approval_id, reason=args.reason, **common)
        _print(result)
    return 0


class _ExecutionReader:
    """Read-only access to execution records (no identity needed to read the audit trail)."""

    def __init__(self, manager: ProposalManager):
        self.repo = manager.repo

    def load(self, proposal_id, execution_id):
        stored, lifecycle, record, lease, events = read_execution_state(self.repo, proposal_id, execution_id)
        if stored is None or record is None:
            raise ProposalNotFoundError(f"execution {execution_id} not found")
        return verify_execution(stored, lifecycle, record, lease, events)

    def history(self, proposal_id):
        return [self.load(proposal_id, r.execution_id) for r in self.repo.list_executions(proposal_id)]


def _cmd_authz(args, parser) -> int:
    if args.authz_cmd == "validate":
        config = parse_authz(args.file.read_text(encoding="utf-8"))
        print(json.dumps({"valid": True, "content_hash": config.content_hash()}, indent=2))
        return 0
    manager = _authz_manager(args, bootstrap=args.authz_cmd == "save")
    if args.authz_cmd == "save":
        config = parse_authz(args.file.read_text(encoding="utf-8"))
        _print(manager.save(config, expected_version=args.expected_version, identity=_identity(args, parser),
                            note=args.note))
    elif args.authz_cmd == "show":
        _print(manager.get_version(args.version) if args.version else manager.get_active())
    elif args.authz_cmd == "history":
        print(json.dumps([{"version": v.version, "content_hash": v.content_hash, "record_hash": v.record_hash,
                           "created_at": v.created_at.isoformat(), "created_by": v.created_by,
                           "mode": v.change.mode.value, "basis": v.change.basis,
                           "changed_accounts": v.change.changed_accounts, "note": v.note}
                          for v in manager.history()], indent=2))
    return 0


def _print(model) -> None:
    print(model.model_dump_json(indent=2))


def _policy(args):
    manager = _manager(args)
    if getattr(args, "policy_version", None) is not None:
        return manager.get_version(args.policy_id, args.policy_version)
    if args.table:
        return manager.get_active(args.policy_id)  # never bootstrap remote state implicitly
    return manager.ensure_default(policy_id=args.policy_id)


def _live_source(args, parser) -> LiveSource:
    session = boto3.Session(profile_name=args.profile)
    regions = args.regions or ([session.region_name] if session.region_name else [])
    if not regions:
        parser.error("no region configured; pass --region")
    return LiveSource(session, regions)


def _cmd_tag(args, parser) -> int:
    """Read-only: interpret a tagging request and print the proposal. Nothing is written to AWS."""
    source = _live_source(args, parser) if args.mode == "live" else FixtureSource(args.fixture)
    outcome = CustomTaggingEngine(source, _policy(args)).prepare(args.request)
    _print(outcome)
    return {OutcomeStatus.PROPOSAL_READY: 0, OutcomeStatus.NO_CHANGES: 0, OutcomeStatus.REJECTED: 2,
            OutcomeStatus.BLOCKED: 3}.get(outcome.status, 1)


def _cmd_scan(args, parser) -> int:
    policy = _policy(args)

    if args.mode == "live":
        source = _live_source(args, parser)
        if args.record:
            discovery = source.discover()
            args.record.write_text(discovery.model_dump_json(indent=2), encoding="utf-8")
            print(f"recorded {len(discovery.resources)} resources to {args.record}", file=sys.stderr)
            source = _Recorded(discovery)
    else:
        source = FixtureSource(args.fixture)

    scan = run_scan(source, policy)
    print(render_summary(scan) if args.output == "summary" else scan.model_dump_json(indent=2))
    return 0


def _cmd_policy(args, parser) -> int:
    if args.policy_cmd == "validate":
        doc = parse_policy(args.file.read_text(encoding="utf-8"))
        print(json.dumps({"valid": True, "content_hash": doc.content_hash()}, indent=2))
        return 0

    manager = _manager(args)
    if args.policy_cmd == "init":
        _print(manager.ensure_default(created_by=args.author, policy_id=args.policy_id))
    elif args.policy_cmd == "show":
        v = (manager.get_version(args.policy_id, args.version) if args.version
             else manager.get_active(args.policy_id))
        _print(v)
    elif args.policy_cmd == "history":
        rows = [{"version": v.version, "content_hash": v.content_hash, "created_at": v.created_at.isoformat(),
                 "created_by": v.created_by, "note": v.note} for v in manager.history(args.policy_id)]
        print(json.dumps(rows, indent=2))
    elif args.policy_cmd == "save":
        doc = parse_policy(args.file.read_text(encoding="utf-8"))
        _print(manager.save(doc, expected_version=args.expected_version, created_by=args.author,
                            note=args.note, policy_id=args.policy_id))
    elif args.policy_cmd == "restore":
        _print(manager.restore(args.version, expected_version=args.expected_version,
                               created_by=args.author, policy_id=args.policy_id))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cloudguard.governance")
    store = argparse.ArgumentParser(add_help=False)
    store.add_argument("--policy-id", default=DEFAULT_POLICY_ID)
    store.add_argument("--policy-store", type=Path, default=DEFAULT_POLICY_DIR, help="Local policy directory")
    store.add_argument("--table", default=None, help="Use the CloudGuard DynamoDB table instead of local files")
    store.add_argument("--profile", default=None, help="AWS profile")

    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", parents=[store], help="Run a tag-compliance scan")
    scan.add_argument("--mode", choices=["fixture", "live"], default="fixture")
    scan.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    scan.add_argument("--region", action="append", dest="regions", default=None,
                      help="Live mode region; repeat for multiple (default: profile/env region)")
    scan.add_argument("--policy-version", type=int, default=None, help="Evaluate against a historical version")
    scan.add_argument("--record", type=Path, default=None, help="Live mode: also save raw discovery as a fixture")
    scan.add_argument("--output", choices=["json", "summary"], default="json",
                      help="json (full scan) or summary (violations / notices / failures as separate sections)")

    tag = sub.add_parser("tag", parents=[store], help="Interpret a tagging request into a proposal (read-only)")
    tag.add_argument("request", help='e.g. "Tag Lambda function ServiceWRK with ServiceWRK=Lambda"')
    tag.add_argument("--mode", choices=["fixture", "live"], default="fixture")
    tag.add_argument("--fixture", type=Path, default=DEFAULT_TAG_FIXTURE)
    tag.add_argument("--region", action="append", dest="regions", default=None)

    prop_store = argparse.ArgumentParser(add_help=False, parents=[store])
    prop_store.add_argument("--proposal-store", type=Path, default=DEFAULT_PROPOSAL_DIR,
                            help="Local proposal directory (ignored with --table)")
    ident = argparse.ArgumentParser(add_help=False)
    ident.add_argument("--mode", choices=["fixture", "live"], default="fixture",
                       help="fixture: development identity; live: AWS caller identity via STS")
    ident.add_argument("--dev-identity", default=None, help="Development identity name (fixture mode only)")
    ident.add_argument("--authz-store", type=Path, default=DEFAULT_AUTHZ_DIR, help="Local authorization config directory")
    decide = argparse.ArgumentParser(add_help=False, parents=[prop_store, ident])
    decide.add_argument("proposal_id")
    decide.add_argument("--content-hash", required=True, help="Exact content hash of the proposal being decided")
    decide.add_argument("--nonce", default=None, help="Single-use nonce (22-128 URL-safe chars); generated if omitted")
    decide.add_argument("--idempotency-key", default=None, help="Replays with the same key return the same decision")

    prop = sub.add_parser("proposal", help="Persist proposals and record authenticated decisions (no execution)")
    prsub = prop.add_subparsers(dest="proposal_cmd", required=True)
    ps = prsub.add_parser("save", parents=[prop_store, ident], help="Build a proposal from a request and store it")
    ps.add_argument("request")
    ps.add_argument("--idempotency-key", default=None, help="8-128 chars [A-Za-z0-9._:-]; replays return the same proposal")
    ps.add_argument("--fixture", type=Path, default=DEFAULT_TAG_FIXTURE)
    ps.add_argument("--fixture-account", type=Path, default=DEFAULT_FIXTURE_ACCOUNT,
                    help="Simulated account state (fixture mode); seeded from --fixture on first use")
    ps.add_argument("--region", action="append", dest="regions", default=None)
    for name in ("show", "history"):
        sp = prsub.add_parser(name, parents=[prop_store])
        sp.add_argument("proposal_id")
    ap = prsub.add_parser("approve", parents=[decide], help="Approve a PENDING proposal")
    ap.add_argument("--acknowledge-overwrites", action="store_true",
                    help="Required when the proposal overwrites existing tag values")
    ap.add_argument("--reason", default="")
    rj = prsub.add_parser("reject", parents=[decide], help="Reject a PENDING proposal")
    rj.add_argument("--reason", required=True)
    rv = prsub.add_parser("revoke", parents=[decide], help="Revoke an APPROVED proposal before execution")
    rv.add_argument("--approval-id", required=True)
    rv.add_argument("--reason", required=True)
    abt = prsub.add_parser("abort", parents=[decide], help="Abort an EXECUTING proposal (needs 'reject')")
    abt.add_argument("--execution-id", required=True)
    abt.add_argument("--reason", required=True)

    ex = prsub.add_parser("execute", parents=[prop_store],
                          help="FIXTURE ONLY: execute an approved proposal against the simulated account")
    ex.add_argument("proposal_id")
    ex.add_argument("--approval-id", required=True)
    ex.add_argument("--mode", choices=["fixture"], required=True,
                    help="Must be 'fixture'; live execution does not exist in this checkpoint")
    ex.add_argument("--dev-identity", required=True, help="Development identity requesting execution")
    ex.add_argument("--authz-store", type=Path, default=DEFAULT_AUTHZ_DIR)
    ex.add_argument("--idempotency-key", default=None)
    ex.add_argument("--fixture", type=Path, default=DEFAULT_TAG_FIXTURE)
    ex.add_argument("--fixture-account", type=Path, default=DEFAULT_FIXTURE_ACCOUNT)
    exs = prsub.add_parser("execution-show", parents=[prop_store], help="Show one execution (simulated)")
    exs.add_argument("proposal_id")
    exs.add_argument("execution_id")
    exh = prsub.add_parser("execution-history", parents=[prop_store], help="List executions of a proposal")
    exh.add_argument("proposal_id")

    az = sub.add_parser("authz", help="Manage the versioned authorization allow-list")
    azsub = az.add_subparsers(dest="authz_cmd", required=True)
    azv = azsub.add_parser("validate")
    azv.add_argument("file", type=Path)
    azstore = argparse.ArgumentParser(add_help=False)
    azstore.add_argument("--authz-store", type=Path, default=DEFAULT_AUTHZ_DIR)
    azstore.add_argument("--table", default=None)
    azstore.add_argument("--profile", default=None)
    azs = azsub.add_parser("save", parents=[azstore])
    azs.add_argument("file", type=Path)
    azs.add_argument("--expected-version", type=int, required=True, help="Active version being replaced (0 if none)")
    azs.add_argument("--mode", choices=["fixture", "live"], default="fixture",
                     help="fixture: change dev: entries as --dev-identity; live: change ARN entries as the STS caller")
    azs.add_argument("--dev-identity", default=None, help="Development identity making the change (fixture mode only)")
    azs.add_argument("--note", default="", help="Free-text note; never an identity")
    azsh = azsub.add_parser("show", parents=[azstore])
    azsh.add_argument("--version", type=int, default=None)
    azsub.add_parser("history", parents=[azstore])

    pol = sub.add_parser("policy", help="Manage versioned tagging policies")
    psub = pol.add_subparsers(dest="policy_cmd", required=True)
    v = psub.add_parser("validate", help="Validate a policy JSON file without saving")
    v.add_argument("file", type=Path)
    i = psub.add_parser("init", parents=[store], help="Create version 1 from the built-in default if missing")
    i.add_argument("--author", default="cloudguard-cli")
    s = psub.add_parser("show", parents=[store], help="Show the active (or a specific) version")
    s.add_argument("--version", type=int, default=None)
    psub.add_parser("history", parents=[store], help="List all versions")
    sv = psub.add_parser("save", parents=[store], help="Validate and save a new version")
    sv.add_argument("file", type=Path)
    sv.add_argument("--expected-version", type=int, required=True,
                    help="Active version you are replacing (0 if none) - guards against concurrent edits")
    sv.add_argument("--author", required=True)
    sv.add_argument("--note", default="")
    r = psub.add_parser("restore", parents=[store], help="Re-activate an old version as a new version")
    r.add_argument("version", type=int)
    r.add_argument("--expected-version", type=int, required=True)
    r.add_argument("--author", required=True)

    args = parser.parse_args(argv)
    if getattr(args, "expected_version", None) == 0:
        args.expected_version = None
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    try:
        commands = {"scan": _cmd_scan, "tag": _cmd_tag, "policy": _cmd_policy, "proposal": _cmd_proposal,
                    "authz": _cmd_authz}
        return commands[args.command](args, parser)
    except (PolicyValidationError, AuthzValidationError) as exc:
        print(json.dumps({"valid": False, "errors": exc.errors}, indent=2), file=sys.stderr)
        return 2
    except (AuthorizationError, IdentityError, DecisionNotAllowedError, NonceReplayError, ExecutionNotAllowedError,
            AuthzDeniedError) as exc:
        print(f"denied: {exc}", file=sys.stderr)
        return 3
    except (PolicyConflictError, PolicyNotFoundError, PolicyIntegrityError, ValueError,
            ProposalNotFoundError, ProposalNotApprovableError, ProposalTooLargeError, IdempotencyConflictError,
            ProposalIntegrityError, TransitionConflictError, AuthzConflictError, AuthzNotFoundError,
            AuthzIntegrityError, ConcurrentExecutionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
