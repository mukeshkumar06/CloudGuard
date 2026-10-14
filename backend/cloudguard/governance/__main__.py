"""CLI for resource governance.

    python -m cloudguard.governance scan   [--mode fixture|live] [--region R ...] [--policy-version N]
    python -m cloudguard.governance tag "REQUEST" [--mode fixture|live]   (read-only proposal)
    python -m cloudguard.governance policy validate FILE
    python -m cloudguard.governance policy init|show|history
    python -m cloudguard.governance policy save FILE --expected-version N --author NAME [--note TEXT]
    python -m cloudguard.governance policy restore VERSION --expected-version N --author NAME

Policies are stored locally under .cloudguard/policies by default, or in the CloudGuard DynamoDB
table with --table. --record saves raw live discovery as a fixture: review it before committing,
because it contains real resource names and tags.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import boto3

from cloudguard.governance.compliance import run_scan
from cloudguard.governance.custom_tagging import CustomTaggingEngine
from cloudguard.governance.custom_tagging.models import OutcomeStatus
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
        commands = {"scan": _cmd_scan, "tag": _cmd_tag, "policy": _cmd_policy}
        return commands[args.command](args, parser)
    except PolicyValidationError as exc:
        print(json.dumps({"valid": False, "errors": exc.errors}, indent=2), file=sys.stderr)
        return 2
    except (PolicyConflictError, PolicyNotFoundError, PolicyIntegrityError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
