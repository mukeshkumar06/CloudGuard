"""Resource discovery sources.

- LiveSource: service-specific boto3 discovery (Resource Groups Tagging API is not used because it
  doesn't reliably return resources that have never been tagged).
- FixtureSource: replays a saved DiscoveryResult JSON, so scans run with no AWS credentials.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from cloudguard.governance.discovery.common import BOTO_CONFIG
from cloudguard.governance.discovery.dynamodb_discovery import discover_dynamodb
from cloudguard.governance.discovery.ec2_discovery import discover_ec2
from cloudguard.governance.discovery.lambda_discovery import discover_lambda
from cloudguard.governance.discovery.s3_discovery import discover_s3
from cloudguard.governance.models import SUPPORTED_RESOURCE_TYPES, DiscoveryResult

if TYPE_CHECKING:  # type hints only; importing FixtureSource must not import boto3
    import boto3

log = logging.getLogger(__name__)

__all__ = ["SUPPORTED_RESOURCE_TYPES", "DiscoverySource", "FixtureSource", "LiveSource"]


class DiscoverySource(Protocol):
    mode: str

    def discover(self) -> DiscoveryResult: ...


class LiveSource:
    """Discovers resources in the authenticated account across one or more regions."""

    mode = "live"

    def __init__(self, session: boto3.Session, regions: list[str]):
        if not regions:
            raise ValueError("at least one region is required")
        self.session = session
        self.regions = list(dict.fromkeys(regions))  # de-duplicate, keep order

    def _client(self, service: str, region: str):
        return self.session.client(service, region_name=region, config=BOTO_CONFIG)

    def discover(self) -> DiscoveryResult:
        now = datetime.now(UTC)
        # The real authenticated account; never taken from configuration.
        account_id = self._client("sts", self.regions[0]).get_caller_identity()["Account"]
        resources, errors = [], []
        for region in self.regions:
            partition = self.session.get_partition_for_region(region)
            for found, errs in (
                discover_lambda(self._client("lambda", region), region, account_id, now),
                discover_dynamodb(self._client("dynamodb", region), region, account_id, partition, now),
                discover_s3(self._client("s3", region), region, account_id, partition, now),
                discover_ec2(self._client("ec2", region), region, account_id, partition, now),
            ):
                resources.extend(found)
                errors.extend(errs)
        # A resource can surface in more than one regional pass (an S3 bucket whose region could
        # not be resolved). Keep the first occurrence so each resource is counted exactly once.
        seen: set[str] = set()
        unique = []
        for r in resources:
            if r.resource_arn not in seen:
                seen.add(r.resource_arn)
                unique.append(r)
        if len(unique) != len(resources):
            log.info("dropped duplicate resources across regions", extra={"duplicates": len(resources) - len(unique)})
        resources = unique
        log.info("discovery complete", extra={"resources": len(resources), "service_errors": len(errors)})
        return DiscoveryResult(account_id=account_id, regions=self.regions, resources=resources, service_errors=errors)


class FixtureSource:
    mode = "fixture"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def discover(self) -> DiscoveryResult:
        return DiscoveryResult.model_validate_json(self.path.read_text(encoding="utf-8"))
