"""Resource discovery sources.

- LiveSource: service-specific boto3 discovery (Resource Groups Tagging API is not used because it
  doesn't reliably return resources that have never been tagged).
- FixtureSource: replays a saved DiscoveryResult JSON, so scans run with no AWS credentials.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import boto3

from cloudguard.governance.discovery.common import BOTO_CONFIG
from cloudguard.governance.discovery.dynamodb_discovery import discover_dynamodb
from cloudguard.governance.discovery.ec2_discovery import discover_ec2
from cloudguard.governance.discovery.lambda_discovery import discover_lambda
from cloudguard.governance.discovery.s3_discovery import discover_s3
from cloudguard.governance.models import DiscoveryResult

log = logging.getLogger(__name__)

SUPPORTED_RESOURCE_TYPES = ["AWS::Lambda::Function", "AWS::DynamoDB::Table", "AWS::S3::Bucket", "AWS::EC2::Instance"]


class DiscoverySource(Protocol):
    mode: str

    def discover(self) -> DiscoveryResult: ...


class LiveSource:
    mode = "live"

    def __init__(self, session: boto3.Session, region: str):
        self.session = session
        self.region = region

    def _client(self, service: str):
        return self.session.client(service, region_name=self.region, config=BOTO_CONFIG)

    def discover(self) -> DiscoveryResult:
        now = datetime.now(UTC)
        account_id = self._client("sts").get_caller_identity()["Account"]
        partition = self.session.get_partition_for_region(self.region)
        resources, errors = [], []
        for found, errs in (
            discover_lambda(self._client("lambda"), self.region, now),
            discover_dynamodb(self._client("dynamodb"), self.region, account_id, partition, now),
            discover_s3(self._client("s3"), self.region, partition, now),
            discover_ec2(self._client("ec2"), self.region, partition, now),
        ):
            resources.extend(found)
            errors.extend(errs)
        log.info("discovery complete", extra={"resources": len(resources), "service_errors": len(errors)})
        return DiscoveryResult(account_id=account_id, region=self.region, resources=resources, service_errors=errors)


class FixtureSource:
    mode = "fixture"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def discover(self) -> DiscoveryResult:
        return DiscoveryResult.model_validate_json(self.path.read_text(encoding="utf-8"))
