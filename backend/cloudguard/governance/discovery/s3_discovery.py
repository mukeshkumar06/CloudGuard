"""Amazon S3 bucket discovery.

ListBuckets is a global call. We pass BucketRegion so a regional scan only returns that region's
buckets, and fall back to GetBucketLocation if a bucket comes back without a BucketRegion.
GetBucketTagging raises NoSuchTagSet for a never-tagged bucket: that is an empty tag set, not a failure.
"""

from __future__ import annotations

import logging
from datetime import datetime

from cloudguard.governance.discovery.common import AWS_ERRORS, classify_error, error_code
from cloudguard.governance.models import UNRESOLVED_REGION, DiscoveredResource, InspectionError, ServiceError

log = logging.getLogger(__name__)

RESOURCE_TYPE = "AWS::S3::Bucket"


def _bucket_region(client, name: str) -> str:
    # GetBucketLocation returns None for us-east-1 and the legacy "EU" for eu-west-1.
    constraint = client.get_bucket_location(Bucket=name).get("LocationConstraint")
    return {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(constraint, constraint)


def discover_s3(
    client, region: str, account_id: str, partition: str, now: datetime
) -> tuple[list[DiscoveredResource], list[ServiceError]]:
    resources: list[DiscoveredResource] = []
    errors: list[ServiceError] = []
    try:
        for page in client.get_paginator("list_buckets").paginate(BucketRegion=region):
            for bucket in page.get("Buckets", []):
                name = bucket["Name"]
                tags: dict[str, str] | None = None
                tag_error: InspectionError | None = None
                bucket_region = bucket.get("BucketRegion")
                if not bucket_region:
                    try:
                        bucket_region = _bucket_region(client, name)
                        if bucket_region != region:
                            continue  # belongs to another region's scan
                    except AWS_ERRORS as exc:
                        # Region unknown: report it once as unresolved; LiveSource de-duplicates by
                        # ARN so a multi-region scan doesn't count it once per region.
                        tag_error = classify_error(exc, "s3:GetBucketLocation")
                        bucket_region = UNRESOLVED_REGION
                if tag_error is None:
                    try:
                        resp = client.get_bucket_tagging(Bucket=name)
                        tags = {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}
                    except AWS_ERRORS as exc:
                        if error_code(exc) == "NoSuchTagSet":
                            tags = {}
                        else:
                            tag_error = classify_error(exc, "s3:GetBucketTagging")
                if tag_error is not None:
                    log.warning("s3 tag inspection failed", extra={"bucket": name, "code": tag_error.code})
                resources.append(
                    DiscoveredResource(
                        account_id=account_id,
                        resource_type=RESOURCE_TYPE,
                        service="s3",
                        resource_id=name,
                        resource_name=name,
                        resource_arn=f"arn:{partition}:s3:::{name}",
                        region=bucket_region,
                        discovered_at=now,
                        tags=tags,
                        tag_source="s3:GetBucketTagging",
                        tag_error=tag_error,
                    )
                )
    except AWS_ERRORS as exc:
        errors.append(ServiceError(service="s3", region=region, error=classify_error(exc, "s3:ListAllMyBuckets")))
    return resources, errors
