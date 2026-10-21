"""Serve the deployment prototype with uvicorn on 127.0.0.1 (PROTOTYPE).

    python -m cloudguard.deploy [--port 8080]
    python -m cloudguard.deploy --new-token

Lambda Web Adapter forwards Function URL requests to this local port (its default is 8080) after
GET /healthz succeeds. The same command runs the app locally for testing. It always binds to 127.0.0.1:
inside Lambda the adapter connects over loopback, and locally nothing else should reach it.
Configuration comes from the environment; see cloudguard.deploy.http_app.
--new-token prints a new random bearer token and the CLOUDGUARD_TOKEN_SHA256 value to configure, then exits.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from cloudguard.deploy.http_app import ConfigError, build_app, new_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cloudguard.deploy", description="CloudGuard MCP Lambda prototype")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--new-token", action="store_true",
                        help="Print a new random bearer token and its SHA-256 digest, then exit")
    args = parser.parse_args(argv)
    if args.new_token:
        token, digest = new_token()
        print(f"token (give to the MCP client; never store it on the server): {token}")
        print(f"CLOUDGUARD_TOKEN_SHA256={digest}")
        return 0
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    try:
        app = build_app()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    uvicorn.run(app, host="127.0.0.1", port=args.port, lifespan="on", access_log=False, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
