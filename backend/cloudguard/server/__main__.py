"""Run the CloudGuard MCP server.

    python -m cloudguard.server [--transport stdio|streamable-http] [--port 8000]
                                [--mode fixture|live] [--fixture NAME|PATH] [--profile NAME] [--max-pages N]
                                [--policy-store DIR] [--operator-evidence FILE]

Defaults: stdio transport, fixture mode (synthetic data, no AWS). Streamable HTTP is stateless, answers
with JSON, serves /mcp and binds to 127.0.0.1 only: there is no MCP authentication yet, and every caller
acts with this process's AWS credentials. Live mode reads Cost Explorer and runs tag-governance discovery
with those credentials (or --profile), read-only; governance covers every region ec2:DescribeRegions
reports as enabled at startup. Fixture mode uses backend/fixtures/governance-demo.json for governance.
Governance tools read the active policy from --policy-store (default .cloudguard/policies, resolved
against the working directory at startup) or, when none is stored, the built-in default in memory;
the server never creates or saves a policy.
--operator-evidence names an operator-maintained JSON file of tag values with their source, used as
evidence by recommend_tags (read on each call, never written; its shape is checked here at startup).
No tool argument can change the account, profile, role or region.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cloudguard.cost.ce import resolve_max_pages
from cloudguard.governance.recommendations import load_operator_evidence
from cloudguard.server.app import create_server
from cloudguard.server.context import POLICY_DIR, CostContext, GovernanceContext
from cloudguard.server.identity_tools import IdentityContext


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cloudguard.server", description="CloudGuard AI MCP server (read-only)")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mode", choices=["fixture", "live"], default="fixture")
    parser.add_argument("--fixture", default=None, help="Fixture mode: cost scenario name or path (default: demo)")
    parser.add_argument("--profile", default=None, help="Live mode: AWS profile for this server process")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--policy-store", type=Path, default=POLICY_DIR,
                        help="Local policy directory to read (never written); default resolved against the cwd")
    parser.add_argument("--operator-evidence", type=Path, default=None,
                        help="Operator tag-value evidence file for recommend_tags (read-only)")
    args = parser.parse_args(argv)
    if args.operator_evidence is not None:
        _, problems = load_operator_evidence(args.operator_evidence)
        if problems:
            parser.error("--operator-evidence: " + "; ".join(problems))
    if args.max_pages is not None:
        resolve_max_pages(args.max_pages)
    if args.mode == "fixture" and args.profile:
        parser.error("--profile applies to live mode only")
    if args.mode == "live" and args.fixture:
        parser.error("--fixture applies to fixture mode only")

    ctx = (CostContext.live(args.profile, args.max_pages) if args.mode == "live"
           else CostContext.fixture(args.fixture, args.max_pages))
    governance = (GovernanceContext.live(args.profile, policy_dir=args.policy_store,
                                         operator_evidence=args.operator_evidence) if args.mode == "live"
                  else GovernanceContext.fixture(policy_dir=args.policy_store,
                                                 operator_evidence=args.operator_evidence))
    print(f"cloudguard-ai MCP server: {ctx.data_source}; {governance.data_source}, transport {args.transport}",
          file=sys.stderr)
    identity = IdentityContext.live(args.profile) if args.mode == "live" else IdentityContext.fixture()
    create_server(ctx, governance, identity, port=args.port).run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
