"""The server's AWS context, fixed once at startup and never chosen by a tool argument.

Live mode uses the server process's own AWS credentials (the default chain, or the profile named on the
command line when the server starts) through the existing read-only Cost Explorer client. No tool can name
an account, profile, role or region, so an MCP client can't redirect the server to another AWS account.
Fixture mode uses synthetic data and never imports boto3.

Trust boundary: there is no MCP-level authentication yet. Anyone who can reach the server acts with the
server's credentials, so the HTTP transport binds to 127.0.0.1 only (see __main__.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

from cloudguard.cost.ce import CE_REGION
from cloudguard.cost.fixture import FixtureCostExplorer


@dataclass(frozen=True)
class CostContext:
    mode: Literal["fixture", "live"]
    client: object  # boto3 Cost Explorer client or FixtureCostExplorer; read-only methods only are called
    data_source: str
    as_of: Callable[[], date]
    max_pages: int | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @classmethod
    def fixture(cls, name: str | None = None, max_pages: int | None = None) -> CostContext:
        client = FixtureCostExplorer.load(name)
        return cls(mode="fixture", client=client, max_pages=max_pages, as_of=lambda: client.as_of,
                   data_source=f"fixture {client.name} (synthetic data, no AWS call)")

    @classmethod
    def live(cls, profile: str | None = None, max_pages: int | None = None) -> CostContext:
        import boto3  # live mode only

        from cloudguard.cost.ce import make_client
        client = make_client(boto3.Session(profile_name=profile))
        return cls(mode="live", client=client, max_pages=max_pages, as_of=lambda: datetime.now(UTC).date(),
                   data_source=f"live AWS Cost Explorer ({CE_REGION}, read-only)")
