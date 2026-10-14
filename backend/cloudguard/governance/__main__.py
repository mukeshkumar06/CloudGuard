"""CLI: python -m cloudguard.governance scan [--mode fixture|live] ...

Prints the scan as JSON. --record saves the raw live discovery as a fixture for replay
(review it before committing: it contains real resource names and tags).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import boto3

from cloudguard.governance.compliance import run_scan
from cloudguard.governance.discovery import FixtureSource, LiveSource
from cloudguard.governance.models import DiscoveryResult
from cloudguard.governance.policy import DEFAULT_POLICY, TagPolicy

DEFAULT_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "governance-demo.json"


class _Recorded:
    """Live discovery that already ran, so --record doesn't call AWS twice."""

    mode = "live"

    def __init__(self, discovery: DiscoveryResult):
        self._discovery = discovery

    def discover(self) -> DiscoveryResult:
        return self._discovery


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cloudguard.governance")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="Run a tag-compliance scan")
    scan.add_argument("--mode", choices=["fixture", "live"], default="fixture")
    scan.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    scan.add_argument("--region", default=None, help="Live mode region (default: profile/env region)")
    scan.add_argument("--profile", default=None, help="Live mode AWS profile")
    scan.add_argument("--policy", type=Path, default=None, help="Tag policy JSON (default: built-in 4 tags)")
    scan.add_argument("--record", type=Path, default=None, help="Live mode: also save raw discovery as a fixture")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    policy = TagPolicy.load(args.policy) if args.policy else DEFAULT_POLICY

    if args.mode == "live":
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        region = session.region_name
        if not region:
            parser.error("no region configured; pass --region")
        source = LiveSource(session, region)
        if args.record:
            discovery = source.discover()
            args.record.write_text(discovery.model_dump_json(indent=2), encoding="utf-8")
            print(f"recorded {len(discovery.resources)} resources to {args.record}", file=sys.stderr)
            source = _Recorded(discovery)
    else:
        source = FixtureSource(args.fixture)

    print(run_scan(source, policy).model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
