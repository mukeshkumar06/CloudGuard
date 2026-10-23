"""Region- and service-scoped discovery, bounded parallel discovery, partial results and the scoped scan cache.

LiveSource runs against fake sessions: either botocore Stubber clients (real request and response shapes) or
discover_* functions replaced by fakes that record timing. MCP calls go through a real client session over the
SDK's in-memory transport against fixture data. Nothing calls AWS, and nothing sleeps for the cache: time is a
fake clock.
"""

import threading
import time
from datetime import UTC, datetime, timedelta

import anyio
import boto3
import pytest
from botocore.stub import Stubber
from mcp.shared.memory import create_connected_server_and_client_session

import cloudguard.governance.discovery as discovery
from cloudguard.governance.discovery import FixtureSource, LiveSource, ScopeError
from cloudguard.governance.models import UNRESOLVED_REGION, DiscoveredResource, ErrorKind, ServiceError
from cloudguard.server.app import create_server
from cloudguard.server.context import GOVERNANCE_FIXTURE, CostContext, GovernanceContext
from cloudguard.server.governance_tools import MIN_REFRESH_INTERVAL, SCAN_MAX_AGE

ACCOUNT = "123456789012"
NOW = datetime(2026, 10, 22, tzinfo=UTC)
REGIONS = ["ap-south-1", "ap-southeast-1", "eu-west-1", "us-east-1"]


# ---------- fakes ----------

class _Sts:
    def __init__(self, account):
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account}


class FakeSession:
    """Clients are opaque tokens: the discover_* functions are replaced, so they're never used for real calls."""

    def __init__(self, account=ACCOUNT):
        self.account = account
        self.created = []

    def client(self, service, region_name, config=None):
        self.created.append((service, region_name))
        return _Sts(self.account) if service == "sts" else (service, region_name)

    def get_partition_for_region(self, region):
        return "aws"


def resource(service, region, name):
    return DiscoveredResource(
        account_id=ACCOUNT, resource_type={"lambda": "AWS::Lambda::Function", "dynamodb": "AWS::DynamoDB::Table",
                                           "s3": "AWS::S3::Bucket", "ec2": "AWS::EC2::Instance"}[service],
        service=service, resource_id=name, resource_name=name, resource_arn=f"arn:aws:{service}:{region}:{ACCOUNT}:{name}",
        region=region, discovered_at=NOW, tags={}, tag_source=f"{service}:tags")


class Recorder:
    """Replaces the four discover_* functions; records calls and how many ran at once."""

    def __init__(self, monkeypatch, delay=0.0, fail=(), hang=(), raise_=()):
        self.calls, self.lock, self.active, self.peak = [], threading.Lock(), 0, 0
        self.release = threading.Event()
        self.delay, self.fail, self.hang, self.raise_ = delay, set(fail), set(hang), set(raise_)
        for service in ("lambda", "dynamodb", "s3", "ec2"):
            monkeypatch.setattr(discovery, f"discover_{service}", self._fake(service))

    def _fake(self, service):
        def run(client, region, *_):
            assert client == (service, region)  # each task uses its own region's client
            with self.lock:
                self.calls.append((service, region))
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                if (service, region) in self.hang:
                    self.release.wait(5)
                time.sleep(self.delay)
                if (service, region) in self.raise_:
                    raise RuntimeError("bug in a discovery module")
                if (service, region) in self.fail:
                    err = discovery.classify_error(Exception("x"), f"{service}:List")
                    return [], [ServiceError(service=service, region=region, error=err.model_copy(
                        update=dict(kind=ErrorKind.ACCESS_DENIED, code="AccessDenied")))]
                return [resource(service, region, f"{service}-{region}")], []
            finally:
                with self.lock:
                    self.active -= 1
        return run


# ---------- LiveSource: scope ----------

def test_service_specific_discovery_queries_only_that_service(monkeypatch):
    rec = Recorder(monkeypatch)
    result = LiveSource(FakeSession(), REGIONS).discover(regions=["ap-south-1"], services=["lambda"])
    assert rec.calls == [("lambda", "ap-south-1")]
    assert result.regions == ["ap-south-1"]
    assert [r.resource_name for r in result.resources] == ["lambda-ap-south-1"]


def test_all_regions_discovers_every_region_and_service_once(monkeypatch):
    rec = Recorder(monkeypatch)
    result = LiveSource(FakeSession(), REGIONS).discover()
    expected = [(s, r) for r in REGIONS for s in ("dynamodb", "ec2", "lambda", "s3")]
    assert sorted(rec.calls) == sorted(expected) and len(rec.calls) == len(set(rec.calls))  # none duplicated
    assert result.regions == REGIONS
    # Results come back in task order, whatever order the threads finished in.
    assert [(r.service, r.region) for r in result.resources] == expected


@pytest.mark.parametrize("regions,services", [(["eu-central-1"], None), (["ap-south-1", "mars-1"], None),
                                              (None, ["iam"])])
def test_scope_can_only_narrow(monkeypatch, regions, services):
    rec = Recorder(monkeypatch)
    with pytest.raises(ScopeError):
        LiveSource(FakeSession(), REGIONS).discover(regions=regions, services=services)
    assert rec.calls == []


# ---------- LiveSource: concurrency ----------

def test_services_run_concurrently_with_a_bounded_pool(monkeypatch):
    rec = Recorder(monkeypatch, delay=0.05)
    LiveSource(FakeSession(), REGIONS, max_workers=3).discover()
    assert rec.peak == 3  # concurrent, and never more than the pool
    assert len(rec.calls) == 16


def test_parallel_discovery_is_faster_than_sequential(monkeypatch):
    rec = Recorder(monkeypatch, delay=0.05)
    started = time.perf_counter()
    LiveSource(FakeSession(), REGIONS, max_workers=1).discover()
    sequential = time.perf_counter() - started
    started = time.perf_counter()
    LiveSource(FakeSession(), REGIONS).discover()
    parallel = time.perf_counter() - started
    assert len(rec.calls) == 32
    assert parallel < sequential / 3  # 16 tasks of 50 ms: ~0.8 s sequential, ~0.1 s on 8 workers


def test_clients_are_created_on_the_calling_thread(monkeypatch):
    Recorder(monkeypatch)
    caller = threading.get_ident()
    threads = set()

    class Session(FakeSession):
        def client(self, service, region_name, config=None):
            threads.add(threading.get_ident())
            return super().client(service, region_name, config)
    LiveSource(Session(), REGIONS).discover()
    assert threads == {caller}  # a boto3 Session is not thread-safe


# ---------- LiveSource: partial results ----------

def test_one_service_failure_keeps_the_others(monkeypatch):
    Recorder(monkeypatch, fail={("s3", "ap-south-1")}, raise_={("ec2", "ap-south-1")})
    result = LiveSource(FakeSession(), ["ap-south-1"]).discover()
    assert sorted(r.service for r in result.resources) == ["dynamodb", "lambda"]
    errors = {(e.service, e.region): e.error for e in result.service_errors}
    assert errors[("s3", "ap-south-1")].kind == ErrorKind.ACCESS_DENIED
    assert errors[("ec2", "ap-south-1")].kind == ErrorKind.UNKNOWN  # an unexpected error is classified, not raised
    assert errors[("ec2", "ap-south-1")].operation == "ec2:discovery"


def test_a_service_past_the_deadline_is_a_timeout_and_the_rest_is_returned(monkeypatch):
    rec = Recorder(monkeypatch, hang={("ec2", "us-east-1")})
    started = time.perf_counter()
    result = LiveSource(FakeSession(), ["ap-south-1", "us-east-1"], deadline_seconds=0.3).discover()
    rec.release.set()
    assert time.perf_counter() - started < 2
    assert len(result.resources) == 7
    (timeout,) = result.service_errors
    assert (timeout.service, timeout.region, timeout.error.kind) == ("ec2", "us-east-1", ErrorKind.TIMEOUT)


# ---------- S3 regions, in a parallel multi-region scan ----------

class StubSession:
    def __init__(self):
        self.clients = {}

    def client(self, service, region_name, config=None):
        key = (service, region_name)
        if key not in self.clients:
            self.clients[key] = boto3.client(service, region_name=region_name,
                                             aws_access_key_id="t", aws_secret_access_key="t")
        return self.clients[key]

    def get_partition_for_region(self, region):
        return "aws"


def test_s3_bucket_regions_stay_correct_in_a_parallel_scan():
    session = StubSession()
    stubs = [Stubber(session.client("sts", "ap-south-1"))]
    stubs[0].add_response("get_caller_identity", {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/u",
                                                  "UserId": "u"})
    # ap-south-1: one bucket reported with BucketRegion, one without (GetBucketLocation says eu-west-1 as "EU",
    # so it belongs to the other pass). eu-west-1: the same legacy bucket, resolved to eu-west-1.
    s3a = Stubber(session.client("s3", "ap-south-1"))
    s3a.add_response("list_buckets", {"Buckets": [{"Name": "mumbai", "BucketRegion": "ap-south-1"},
                                                  {"Name": "legacy"}], "Owner": {"ID": "o"}},
                     {"BucketRegion": "ap-south-1"})
    s3a.add_response("get_bucket_tagging", {"TagSet": [{"Key": "Owner", "Value": "a"}]}, {"Bucket": "mumbai"})
    s3a.add_response("get_bucket_location", {"LocationConstraint": "EU"}, {"Bucket": "legacy"})
    s3b = Stubber(session.client("s3", "eu-west-1"))
    s3b.add_response("list_buckets", {"Buckets": [{"Name": "legacy"}], "Owner": {"ID": "o"}},
                     {"BucketRegion": "eu-west-1"})
    s3b.add_response("get_bucket_location", {"LocationConstraint": "EU"}, {"Bucket": "legacy"})
    s3b.add_response("get_bucket_tagging", {"TagSet": []}, {"Bucket": "legacy"})
    stubs += [s3a, s3b]
    for s in stubs:
        s.activate()
    try:
        result = LiveSource(session, ["ap-south-1", "eu-west-1"]).discover(services=["s3"])
    finally:
        for s in stubs:
            s.deactivate()
    for s in stubs:
        s.assert_no_pending_responses()
    assert sorted((r.resource_name, r.region) for r in result.resources) == [("legacy", "eu-west-1"),
                                                                            ("mumbai", "ap-south-1")]
    assert result.service_errors == []


# ---------- FixtureSource scope ----------

def test_fixture_scope_filters_regions_and_services():
    full = FixtureSource(GOVERNANCE_FIXTURE).discover()
    region = full.regions[0]
    only = FixtureSource(GOVERNANCE_FIXTURE).discover(regions=[region], services=["lambda"])
    assert only.regions == [region]
    assert {r.service for r in only.resources} == {"lambda"}
    assert len(only.resources) == sum(1 for r in full.resources if r.service == "lambda"
                                      and r.region in (region, UNRESOLVED_REGION))
    with pytest.raises(ScopeError):
        FixtureSource(GOVERNANCE_FIXTURE).discover(regions=["eu-central-1"])


# ---------- MCP: scoped inventory and the scoped cache ----------

class ScopedCountingSource:
    """Fixture data; records each discover() scope. `account` can change to simulate another identity."""

    mode = "fixture"

    def __init__(self):
        self.inner = FixtureSource(GOVERNANCE_FIXTURE)
        self.scopes = []
        self.account = None

    def discover(self, regions=None, services=None):
        self.scopes.append((tuple(regions or ()), tuple(services or ())))
        result = self.inner.discover(regions=regions, services=services)
        return result.model_copy(update={"account_id": self.account}) if self.account else result


def ctx_with(tmp_path, now):
    src = ScopedCountingSource()
    ctx = GovernanceContext(mode="fixture", source=src, regions=("ap-south-1",), data_source="scoped fixture",
                            policy_dir=tmp_path / "policies", clock=lambda: now[0])
    return ctx, src


def run_calls(ctx, steps):
    """Run (name, args) steps, or callables (for clock changes), against ONE server."""
    async def main():
        async with create_connected_server_and_client_session(create_server(CostContext.fixture(), ctx)) as client:
            out = []
            for step in steps:
                if callable(step):
                    step()
                else:
                    r = await client.call_tool(*step)
                    out.append(r)
            return out
    return anyio.run(main)


def advance(now, delta):
    return lambda: now.__setitem__(0, now[0] + delta)


def test_scoped_inventory_scans_only_the_scope(tmp_path):
    now = [datetime.now(UTC)]
    ctx, src = ctx_with(tmp_path, now)
    (r,) = run_calls(ctx, [("get_resource_inventory", {"region": "ap-south-1", "services": ["lambda"]})])
    out = r.structuredContent
    assert src.scopes == [(("ap-south-1",), ("lambda",))]
    assert out["regions_scanned"] == ["ap-south-1"] and out["services_scanned"] == ["lambda"]
    assert set(out["by_service"]) == {"lambda"}


def test_region_all_or_all_services_is_the_full_scan(tmp_path):
    now = [datetime.now(UTC)]
    ctx, src = ctx_with(tmp_path, now)
    a, b = run_calls(ctx, [("get_resource_inventory", {"region": "all"}),
                           ("get_resource_inventory", {"services": ["dynamodb", "ec2", "lambda", "s3"]})])
    assert src.scopes == [((), ())]  # one full scan, reused by the second call
    assert b.structuredContent["freshness"]["scan_reused"]


def test_a_region_that_is_not_enabled_is_refused(tmp_path):
    now = [datetime.now(UTC)]
    ctx, src = ctx_with(tmp_path, now)
    (r,) = run_calls(ctx, [("get_resource_inventory", {"region": "eu-central-1"})])
    assert r.isError and "region not enabled for this account: eu-central-1" in r.content[0].text


def test_cache_hit_expiry_and_forced_refresh(tmp_path):
    now = [datetime.now(UTC)]
    ctx, src = ctx_with(tmp_path, now)
    scope = {"region": "ap-south-1"}
    # Scans are stamped with real time and the server's clock is fake, so ages are measured from the start: every
    # scan here completes at (real) t0.
    out = run_calls(ctx, [
        ("get_resource_inventory", scope),  # t0: a scan
        ("get_resource_inventory", scope | {"refresh": True}),  # t0: refresh deferred (under 10 s)
        advance(now, MIN_REFRESH_INTERVAL + timedelta(seconds=1)),
        ("get_resource_inventory", scope | {"refresh": True}),  # t0+11 s: refresh bypasses the 60 s cache
        advance(now, SCAN_MAX_AGE - MIN_REFRESH_INTERVAL - timedelta(seconds=2)),
        ("get_resource_inventory", scope),  # t0+59 s: reused
        advance(now, timedelta(seconds=2)),
        ("get_resource_inventory", scope),  # t0+61 s: expired, a new scan
    ])
    f = [r.structuredContent["freshness"] for r in out]
    assert SCAN_MAX_AGE == timedelta(seconds=60)
    assert [x["scan_reused"] for x in f] == [False, True, False, True, False]
    assert f[1]["refresh_deferred"] is True and f[2]["refresh_deferred"] is False
    assert len(src.scopes) == 3 and f[0]["max_scan_age_seconds"] == 60


def test_cache_is_separate_per_region_and_service_scope(tmp_path):
    now = [datetime.now(UTC)]
    ctx, src = ctx_with(tmp_path, now)
    run_calls(ctx, [("get_resource_inventory", {"region": "ap-south-1", "services": ["lambda"]}),
                    ("get_resource_inventory", {"region": "ap-south-1", "services": ["s3"]}),
                    ("get_resource_inventory", {"region": "ap-south-1", "services": ["lambda"]})])
    assert src.scopes == [(("ap-south-1",), ("lambda",)), (("ap-south-1",), ("s3",))]


def test_a_fresh_full_scan_answers_scoped_questions_without_aws(tmp_path):
    now = [datetime.now(UTC)]
    ctx, src = ctx_with(tmp_path, now)
    full, scoped = run_calls(ctx, [("scan_tag_compliance", {}),
                                   ("get_resource_inventory", {"region": "ap-south-1", "services": ["s3"]})])
    assert src.scopes == [((), ())]
    out = scoped.structuredContent
    assert out["freshness"]["scan_reused"] and out["services_scanned"] == ["s3"]
    assert out["total_resources"] == out["by_service"]["s3"]


def test_cached_scans_are_never_shared_across_accounts(tmp_path):
    now = [datetime.now(UTC)]
    ctx, src = ctx_with(tmp_path, now)

    def switch():
        src.account = "999988887777"
    out = run_calls(ctx, [
        ("get_resource_inventory", {"region": "ap-south-1", "services": ["lambda"]}),
        ("get_resource_inventory", {"region": "ap-south-1", "services": ["s3"]}),
        switch,
        advance(now, MIN_REFRESH_INTERVAL + timedelta(seconds=1)),
        ("get_resource_inventory", {"region": "ap-south-1", "services": ["s3"], "refresh": True}),
        ("get_resource_inventory", {"region": "ap-south-1", "services": ["lambda"]}),
    ])
    accounts = [r.structuredContent["account_id"] for r in out]
    assert accounts == [ACCOUNT, ACCOUNT, "999988887777", "999988887777"]
    assert len(src.scopes) == 4  # the other account's cached lambda scan was dropped, not reused


def test_partial_inventory_is_never_complete(tmp_path):
    now = [datetime.now(UTC)]
    ctx, src = ctx_with(tmp_path, now)
    inner = src.inner.discover()
    failing = ServiceError(service="ec2", region=inner.regions[0], error=discovery.classify_error(
        Exception("x"), "ec2:discovery").model_copy(update=dict(kind=ErrorKind.TIMEOUT, code="DiscoveryTimeout")))
    src.inner.discover = lambda regions=None, services=None: inner.model_copy(update={"service_errors": [failing]})
    (r,) = run_calls(ctx, [("get_resource_inventory", {})])
    out = r.structuredContent
    assert out["coverage_complete"] is False and out["outcome"] == "findings"
    assert out["discovery_errors"] == [{"service": "ec2", "region": inner.regions[0],
                                        "error": {"kind": "TIMEOUT", "code": "DiscoveryTimeout",
                                                  "operation": "ec2:discovery"}}]
    assert out["total_resources"] == len(inner.resources)  # what did complete is still returned
    assert "incomplete" in out["speech"]
