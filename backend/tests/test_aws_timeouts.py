"""Explicit AWS timeouts and call budgets (QA audit #68): every AWS call a tool can make is bounded so a tool call
stays under the simulator's 60-second MCP call timeout. Fake sessions and fixture clients only; no AWS."""

import time
from datetime import UTC, datetime, timedelta

import anyio

from mcp.shared.memory import create_connected_server_and_client_session

import cloudguard.governance.discovery as discovery
import cloudguard.server.cost_tools as cost_tools
from cloudguard.cost.ce import CE_CONFIG
from cloudguard.governance.discovery import DISCOVERY_DEADLINE_SECONDS, LiveSource
from cloudguard.governance.discovery.common import BOTO_CONFIG, SINGLE_CALL_CONFIG
from cloudguard.governance.models import ErrorKind
from cloudguard.server.app import create_server
from cloudguard.server.context import CostContext, DeferredLiveSource, GovernanceContext
from cloudguard.server.identity_tools import IdentityContext

SIMULATOR_CALL_TIMEOUT = 60.0  # web/src/lib/mcp.ts CALL_TIMEOUT_MS


def attempts(config) -> int:
    """Total attempts. botocore's max_attempts counts retries after the first try, and creating a client rewrites
    a shared Config in place to total_max_attempts (max_attempts + 1), so accept either form."""
    retries = config.retries
    return retries["total_max_attempts"] if "total_max_attempts" in retries else retries["max_attempts"] + 1


def worst_case(config) -> float:
    """Upper bound on one botocore call: every attempt hits both timeouts (backoff is capped well under 1 s)."""
    return attempts(config) * (config.connect_timeout + config.read_timeout + 1)


def test_every_config_sets_explicit_timeouts():
    for config in (BOTO_CONFIG, SINGLE_CALL_CONFIG, CE_CONFIG):
        assert config.connect_timeout is not None and config.read_timeout is not None
        assert config.connect_timeout <= 5 and config.read_timeout <= 15  # botocore's defaults are 60 s each


def test_first_live_scan_stays_under_the_simulator_timeout():
    # DescribeRegions (first scan only) + the discovery deadline, which already includes sts:GetCallerIdentity.
    assert worst_case(SINGLE_CALL_CONFIG) + DISCOVERY_DEADLINE_SECONDS < SIMULATOR_CALL_TIMEOUT
    assert worst_case(SINGLE_CALL_CONFIG) < DISCOVERY_DEADLINE_SECONDS  # STS fits inside the deadline


def test_cost_call_budget_is_under_the_simulator_timeout():
    assert cost_tools.COST_CALL_BUDGET_SECONDS < SIMULATOR_CALL_TIMEOUT


class RecordingSession:
    def __init__(self, sts_delay=0.0):
        self.configs, self.sts_delay = {}, sts_delay

    def client(self, service, region_name=None, config=None):
        self.configs[service] = config
        session = self

        class Client:
            def get_caller_identity(self):
                time.sleep(session.sts_delay)
                return {"Account": "123456789012"}

            def describe_regions(self, AllRegions=False):
                return {"Regions": [{"RegionName": "ap-south-1", "OptInStatus": "opt-in-not-required"}]}
        return Client()

    def get_partition_for_region(self, region):
        return "aws"


def fake_discovery(monkeypatch, delay):
    def make(service):
        def run(client, region, *_):
            time.sleep(delay)
            return [], []
        return run
    for service in ("lambda", "dynamodb", "s3", "ec2"):
        monkeypatch.setattr(discovery, f"discover_{service}", make(service))


def test_sts_and_describe_regions_use_the_single_call_config(monkeypatch):
    fake_discovery(monkeypatch, 0)
    session = RecordingSession()
    DeferredLiveSource(lambda: session).discover(services=["lambda"])  # no ec2 discovery client to overwrite it
    assert session.configs["ec2"] is SINGLE_CALL_CONFIG  # ec2:DescribeRegions on the first scan
    assert session.configs["sts"] is SINGLE_CALL_CONFIG
    assert session.configs["lambda"] is BOTO_CONFIG


def test_the_deadline_includes_the_sts_call(monkeypatch):
    fake_discovery(monkeypatch, 0.4)
    started = time.perf_counter()
    result = LiveSource(RecordingSession(sts_delay=0.4), ["ap-south-1"], deadline_seconds=0.6).discover()
    elapsed = time.perf_counter() - started
    assert elapsed < 0.9  # 0.4 s STS + what is left of the 0.6 s deadline, not 0.4 + 0.6
    assert {e.error.kind for e in result.service_errors} == {ErrorKind.TIMEOUT}
    assert len(result.service_errors) == 4  # partial: every unfinished service is reported, none silently dropped


def test_a_cost_call_past_its_budget_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(cost_tools, "COST_CALL_BUDGET_SECONDS", 0.2)
    ctx = CostContext.fixture()
    slow = ctx.client
    original = type(slow).get_cost_and_usage

    def get_cost_and_usage(self, **kwargs):
        time.sleep(0.6)
        return original(self, **kwargs)
    monkeypatch.setattr(type(slow), "get_cost_and_usage", get_cost_and_usage)

    async def main():
        async with create_connected_server_and_client_session(create_server(ctx)) as client:
            started = time.perf_counter()
            result = await client.call_tool("get_monthly_aws_bill", {})
            return result, time.perf_counter() - started
    result, elapsed = anyio.run(main)
    assert result.isError and "did not answer within 0.2 seconds" in result.content[0].text
    assert elapsed < 0.6  # the client was answered at the budget, not when the slow call finished


def test_identity_sts_client_is_bounded():
    session = RecordingSession()
    IdentityContext.live(session_factory=lambda: session).sts_factory()
    config = session.configs["sts"]
    assert config.connect_timeout is not None and config.read_timeout is not None
    assert worst_case(config) < SIMULATOR_CALL_TIMEOUT


def test_inventory_says_when_a_refresh_was_deferred(tmp_path):
    now = [datetime.now(UTC)]
    g = GovernanceContext.fixture(policy_dir=tmp_path / "p")
    g = GovernanceContext(mode=g.mode, source=g.source, regions=g.regions, data_source=g.data_source,
                          policy_dir=g.policy_dir, clock=lambda: now[0])

    async def main():
        async with create_connected_server_and_client_session(create_server(CostContext.fixture(), g)) as client:
            await client.call_tool("get_resource_inventory", {"region": "ap-south-1"})
            deferred = await client.call_tool("get_resource_inventory", {"region": "ap-south-1", "refresh": True})
            now[0] += timedelta(seconds=11)
            fresh = await client.call_tool("get_resource_inventory", {"region": "ap-south-1", "refresh": True})
            return deferred.structuredContent, fresh.structuredContent
    deferred, fresh = anyio.run(main)
    assert deferred["freshness"]["refresh_deferred"] is True
    assert deferred["speech"].startswith("A new scan was not started because the last one finished less than 10 s")
    assert fresh["freshness"]["scan_reused"] is False and not fresh["speech"].startswith("A new scan was not")
