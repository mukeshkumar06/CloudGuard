"""The server's AWS context, fixed once at startup and never chosen by a tool argument.

Live mode uses the server process's own AWS credentials (the default chain, or the profile named on the
command line when the server starts) through the existing read-only Cost Explorer client and the
read-only governance discovery. Governance scans cover every region enabled for the account, listed
once with ec2:DescribeRegions at startup. No tool can name an account, profile, role or region, so an
MCP client can't redirect the server to another AWS account. Fixture mode uses synthetic data and never
imports boto3.

Trust boundary: there is no MCP-level authentication yet. Anyone who can reach the server acts with the
server's credentials, so the HTTP transport binds to 127.0.0.1 only (see __main__.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from cloudguard.cost.ce import CE_REGION
from cloudguard.cost.fixture import FixtureCostExplorer
from cloudguard.governance.discovery import DiscoverySource, FixtureSource, LiveSource
from cloudguard.governance.discovery.common import BOTO_CONFIG
from cloudguard.governance.policy import DEFAULT_POLICY_DOCUMENT, DEFAULT_POLICY_ID, PolicyVersion
from cloudguard.governance.policy_store import LocalPolicyStore, PolicyManager


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


GOVERNANCE_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "governance-demo.json"
# The CLI's default local policy store. It is relative, so GovernanceContext resolves it against the
# server's working directory once, at startup; later cwd changes never switch the policy.
POLICY_DIR = Path(".cloudguard") / "policies"
_BUILT_IN_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def enabled_regions(ec2_client) -> list[str]:
    """Regions enabled for this account (ec2:DescribeRegions), sorted. Opt-in regions not opted into are left out."""
    regions = ec2_client.describe_regions(AllRegions=False).get("Regions", [])
    return sorted(r["RegionName"] for r in regions if r.get("OptInStatus") != "not-opted-in")


class DeferredLiveSource:
    """A LiveSource built on the first discover(): session and enabled regions are resolved then, not at
    startup. Only the governance scan coordinator calls discover(), one scan at a time."""

    mode = "live"

    def __init__(self, session_factory: Callable[[], object]):
        self._session_factory = session_factory
        self._source: LiveSource | None = None

    def discover(self):
        if self._source is None:
            session = self._session_factory()
            home = getattr(session, "region_name", None) or "us-east-1"
            regions = enabled_regions(session.client("ec2", region_name=home, config=BOTO_CONFIG))
            if not regions:
                raise RuntimeError("ec2:DescribeRegions returned no enabled regions")
            self._source = LiveSource(session, regions)
        return self._source.discover()


@dataclass(frozen=True)
class GovernanceContext:
    """Tag-governance context, fixed at startup: a discovery source over every enabled region.

    The policy is read on each scan, read-only: the active version in the local policy store when one
    exists, else the built-in default held in memory. The server never creates or saves a policy.
    `policy_dir` is made absolute when the context is built (relative paths resolve against the
    working directory at that moment); None means the built-in default only.

    `operator_evidence` is an optional operator-maintained JSON file of tag values with their source
    (see cloudguard.governance.recommendations), read-only, made absolute the same way and read on each
    recommend_tags call. Only the server operator sets it; no tool argument can name or change it.
    """

    mode: Literal["fixture", "live"]
    source: DiscoverySource
    regions: tuple[str, ...]
    data_source: str
    policy_dir: Path | None = POLICY_DIR
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    operator_evidence: Path | None = None

    def __post_init__(self) -> None:
        for name in ("policy_dir", "operator_evidence"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, Path(getattr(self, name)).resolve())

    @classmethod
    def fixture(cls, path: str | Path | None = None, policy_dir: Path | None = POLICY_DIR,
                operator_evidence: Path | None = None) -> GovernanceContext:
        path = Path(path) if path else GOVERNANCE_FIXTURE
        source = FixtureSource(path)
        regions = tuple(source.discover().regions)
        return cls(mode="fixture", source=source, regions=regions, policy_dir=policy_dir,
                   operator_evidence=operator_evidence,
                   data_source=f"fixture {path.name} (synthetic data, no AWS call)")

    @classmethod
    def live(cls, profile: str | None = None, policy_dir: Path | None = POLICY_DIR,
             session=None, operator_evidence: Path | None = None) -> GovernanceContext:
        if session is None:
            import boto3  # live mode only
            session = boto3.Session(profile_name=profile)
        home = getattr(session, "region_name", None) or "us-east-1"
        regions = enabled_regions(session.client("ec2", region_name=home, config=BOTO_CONFIG))
        if not regions:
            raise RuntimeError("ec2:DescribeRegions returned no enabled regions")
        return cls(mode="live", source=LiveSource(session, regions), regions=tuple(regions), policy_dir=policy_dir,
                   operator_evidence=operator_evidence,
                   data_source=f"live AWS discovery ({len(regions)} enabled regions, read-only)")

    @classmethod
    def live_deferred(cls, profile: str | None = None, policy_dir: Path | None = POLICY_DIR,
                      session_factory: Callable[[], object] | None = None,
                      operator_evidence: Path | None = None) -> GovernanceContext:
        """Live context that makes no AWS call until the first scan.

        For servers reachable over a network: the boto3 session is created and the enabled regions are
        listed (ec2:DescribeRegions) inside the first scan, which runs only after the caller has been
        authenticated and authorized. `regions` is empty until then; each scan reports its own regions.
        """
        def default_factory():
            import boto3  # live mode only
            return boto3.Session(profile_name=profile)
        return cls(mode="live", source=DeferredLiveSource(session_factory or default_factory), regions=(),
                   policy_dir=policy_dir, operator_evidence=operator_evidence,
                   data_source="live AWS discovery (enabled regions listed at the first scan, read-only)")

    def policy(self) -> tuple[PolicyVersion, Literal["stored", "built-in default"]]:
        """The active stored policy, or the built-in default in memory. Never writes."""
        if self.policy_dir is not None:
            store = LocalPolicyStore(self.policy_dir)
            if store.active_version(DEFAULT_POLICY_ID) is not None:
                return PolicyManager(store).get_active(DEFAULT_POLICY_ID), "stored"
        built_in = PolicyVersion(policy_id=DEFAULT_POLICY_ID, version=1, document=DEFAULT_POLICY_DOCUMENT,
                                 content_hash=DEFAULT_POLICY_DOCUMENT.content_hash(),
                                 created_at=_BUILT_IN_CREATED_AT, created_by="cloudguard-built-in",
                                 note="built-in default policy (in memory, not saved)")
        return built_in, "built-in default"
