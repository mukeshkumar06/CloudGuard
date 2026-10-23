"""Resource discovery sources.

- LiveSource: service-specific boto3 discovery (Resource Groups Tagging API is not used because it
  doesn't reliably return resources that have never been tagged).
- FixtureSource: replays a saved DiscoveryResult JSON, so scans run with no AWS credentials.

Both can be narrowed to some regions and services (`discover(regions=..., services=...)`); by default they
cover every region the source was built for and every supported service. A scope can only narrow a scan:
a region outside the source's own list, or an unsupported service, is refused with ScopeError.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from cloudguard.governance.discovery.common import BOTO_CONFIG, SINGLE_CALL_CONFIG, classify_error
from cloudguard.governance.discovery.dynamodb_discovery import discover_dynamodb
from cloudguard.governance.discovery.ec2_discovery import discover_ec2
from cloudguard.governance.discovery.lambda_discovery import discover_lambda
from cloudguard.governance.discovery.s3_discovery import discover_s3
from cloudguard.governance.models import (
    SUPPORTED_RESOURCE_TYPES, UNRESOLVED_REGION, DiscoveredResource, DiscoveryResult, ErrorKind, InspectionError,
    ServiceError,
)

if TYPE_CHECKING:  # type hints only; importing FixtureSource must not import boto3
    import boto3

log = logging.getLogger(__name__)

__all__ = ["SUPPORTED_RESOURCE_TYPES", "SUPPORTED_SERVICES", "DiscoverySource", "FixtureSource", "LiveSource",
           "ScopeError"]

SUPPORTED_SERVICES = ("dynamodb", "ec2", "lambda", "s3")

# Discovery runs one task per (region, service) on a private pool of at most MAX_WORKERS threads, so a scan never
# has more than MAX_WORKERS discovery tasks (and AWS requests) in flight, however many regions are enabled. The
# deadline counts from the start of discover() (sts:GetCallerIdentity included): a task still running when it
# passes is reported as a TIMEOUT service error and its results are dropped; the botocore timeouts in BOTO_CONFIG
# bound how long such a task keeps running in the background. With ec2:DescribeRegions on a live server's first
# scan (SINGLE_CALL_CONFIG, about 17 s at worst), a scan stays under the simulator's 60-second call timeout.
MAX_WORKERS = 8
DISCOVERY_DEADLINE_SECONDS = 35.0


class ScopeError(ValueError):
    """A requested region or service is outside what this source discovers."""


class DiscoverySource(Protocol):
    mode: str

    def discover(self, regions: list[str] | None = None, services: list[str] | None = None) -> DiscoveryResult: ...


def resolve_scope(available: list[str], regions: list[str] | None,
                  services: list[str] | None) -> tuple[list[str], list[str]]:
    """The regions and services to discover: all of them when not given. Can only narrow, never widen."""
    regions = list(dict.fromkeys(regions)) if regions else list(available)
    unknown = [r for r in regions if r not in available]
    if unknown:
        raise ScopeError(f"region not enabled for this account: {', '.join(unknown)}")
    services = [s for s in SUPPORTED_SERVICES if s in services] if services else list(SUPPORTED_SERVICES)
    if not services:
        raise ScopeError("no supported service requested")
    return regions, services


def _dedupe(resources: list[DiscoveredResource]) -> list[DiscoveredResource]:
    # A resource can surface in more than one regional pass (an S3 bucket whose region could not be
    # resolved). Keep the first occurrence so each resource is counted exactly once.
    seen: set[str] = set()
    unique = []
    for r in resources:
        if r.resource_arn not in seen:
            seen.add(r.resource_arn)
            unique.append(r)
    if len(unique) != len(resources):
        log.info("dropped duplicate resources across regions", extra={"duplicates": len(resources) - len(unique)})
    return unique


class LiveSource:
    """Discovers resources in the authenticated account across one or more regions."""

    mode = "live"

    def __init__(self, session: boto3.Session, regions: list[str], max_workers: int = MAX_WORKERS,
                 deadline_seconds: float = DISCOVERY_DEADLINE_SECONDS):
        if not regions:
            raise ValueError("at least one region is required")
        self.session = session
        self.regions = list(dict.fromkeys(regions))  # de-duplicate, keep order
        self.max_workers = max_workers
        self.deadline_seconds = deadline_seconds

    def _client(self, service: str, region: str):
        return self.session.client(service, region_name=region, config=BOTO_CONFIG)

    def _task(self, service: str, region: str, account_id: str, now: datetime):
        client = self._client(service, region)
        if service == "lambda":
            return lambda: discover_lambda(client, region, account_id, now)
        partition = self.session.get_partition_for_region(region)
        fn = {"dynamodb": discover_dynamodb, "s3": discover_s3, "ec2": discover_ec2}[service]
        return lambda: fn(client, region, account_id, partition, now)

    def discover(self, regions: list[str] | None = None, services: list[str] | None = None) -> DiscoveryResult:
        regions, services = resolve_scope(self.regions, regions, services)
        started = time.monotonic()
        now = datetime.now(UTC)
        # The real authenticated account; never taken from configuration.
        sts = self.session.client("sts", region_name=regions[0], config=SINGLE_CALL_CONFIG)
        account_id = sts.get_caller_identity()["Account"]
        # Clients are created here, on this thread (a boto3 Session is not thread-safe); each task then uses only
        # its own client, which is. No task depends on another's result. S3's ListBuckets is a global call, but
        # each regional task passes BucketRegion, so every task returns only its own region's buckets.
        tasks = [(region, service, self._task(service, region, account_id, now))
                 for region in regions for service in services]
        resources, errors = [], []
        pool = ThreadPoolExecutor(max_workers=min(self.max_workers, len(tasks)), thread_name_prefix="cg-discovery")
        try:
            futures = [pool.submit(call) for _, _, call in tasks]
            wait(futures, timeout=max(0.0, self.deadline_seconds - (time.monotonic() - started)))
            # Collected in task order, not completion order, so the result is the same however the threads ran.
            for (region, service, _), future in zip(tasks, futures, strict=True):
                if not future.done():
                    future.cancel()
                    errors.append(ServiceError(service=service, region=region, error=InspectionError(
                        kind=ErrorKind.TIMEOUT, code="DiscoveryTimeout", operation=f"{service}:discovery",
                        message=f"discovery did not finish within {self.deadline_seconds:g} seconds")))
                    continue
                try:
                    found, errs = future.result()
                except Exception as exc:  # one service's unexpected failure never discards the others
                    errors.append(ServiceError(service=service, region=region,
                                               error=classify_error(exc, f"{service}:discovery")))
                    continue
                resources.extend(found)
                errors.extend(errs)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)  # queued tasks are cancelled; nothing is left waiting
        resources = _dedupe(resources)
        log.info("discovery complete", extra={"resources": len(resources), "service_errors": len(errors)})
        return DiscoveryResult(account_id=account_id, regions=regions, resources=resources, service_errors=errors)


class FixtureSource:
    mode = "fixture"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def discover(self, regions: list[str] | None = None, services: list[str] | None = None) -> DiscoveryResult:
        full = DiscoveryResult.model_validate_json(self.path.read_text(encoding="utf-8"))
        if not regions and not services:
            return full
        regions, services = resolve_scope(full.regions, regions, services)
        # A bucket whose region could not be resolved may belong to any region; a live regional pass reports it
        # too, so a regional fixture scan keeps it.
        def in_scope(service: str, region: str) -> bool:
            return service in services and region in (*regions, UNRESOLVED_REGION)
        return full.model_copy(update=dict(
            regions=regions,
            resources=[r for r in full.resources if in_scope(r.service, r.region)],
            service_errors=[e for e in full.service_errors if in_scope(e.service, e.region)]))
