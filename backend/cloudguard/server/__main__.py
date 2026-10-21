"""Run the CloudGuard MCP server.

    python -m cloudguard.server [--transport stdio|streamable-http] [--port 8000]
                                [--mode fixture|live] [--fixture NAME|PATH] [--profile NAME] [--max-pages N]

Defaults: stdio transport, fixture mode (synthetic data, no AWS). Streamable HTTP is stateless, answers
with JSON, serves /mcp and binds to 127.0.0.1 only: there is no MCP authentication yet, and every caller
acts with this process's AWS credentials. Live mode reads Cost Explorer with those credentials (or
--profile), read-only. No tool argument can change the account, profile, role or region.
"""

from __future__ import annotations

import argparse
import sys

from cloudguard.cost.ce import resolve_max_pages
from cloudguard.server.app import create_server
from cloudguard.server.context import CostContext


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cloudguard.server", description="CloudGuard AI MCP server (read-only)")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mode", choices=["fixture", "live"], default="fixture")
    parser.add_argument("--fixture", default=None, help="Fixture mode: cost scenario name or path (default: demo)")
    parser.add_argument("--profile", default=None, help="Live mode: AWS profile for this server process")
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args(argv)
    if args.max_pages is not None:
        resolve_max_pages(args.max_pages)
    if args.mode == "fixture" and args.profile:
        parser.error("--profile applies to live mode only")
    if args.mode == "live" and args.fixture:
        parser.error("--fixture applies to fixture mode only")

    ctx = (CostContext.live(args.profile, args.max_pages) if args.mode == "live"
           else CostContext.fixture(args.fixture, args.max_pages))
    print(f"cloudguard-ai MCP server: {ctx.data_source}, transport {args.transport}", file=sys.stderr)
    create_server(ctx, port=args.port).run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
