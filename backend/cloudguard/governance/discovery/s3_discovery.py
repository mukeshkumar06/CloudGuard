"""Amazon S3 bucket discovery.

ListBuckets is a global call; we filter to the scanned region with its BucketRegion parameter so a
regional scan only reports that region's buckets. GetBucketTagging raises NoSuchTagSet when a bucket
has never been tagged, which is an empty tag set, not an inspection failure.
"""

from __future__ import annotations

import logging
from datetime import datetime

from cloudguard.governance.discovery.common import AWS_ERRORS, classify_error, error_code
from cloudguard.governance.models import DiscoveredResource, ServiceError

log = logging.getLogger(__name__)

RESOURCE_TYPE = "AWS::S3::Bucket"


def discover_s3(client, region: str, partition: str, now: datetime) -> tuple[list[DiscoveredResource], list[ServiceError]]:
    resources: list[DiscoveredResource] = []
    errors: list[ServiceError] = []
    try:
        for page in client.get_paginator("list_buckets").paginate(BucketRegion=region):
            for bucket in page.get("Buckets", []):
                name = bucket["Name"]
                tags, tag_error = None, None
                try:
                    tags = {t["Key"]: t["Value"] for t in client.get_bucket_tagging(Bucket=name).get("TagSet", [])}
                except AWS_ERRORS as exc:
                    if error_code(exc) == "NoSuchTagSet":
                        tags = {}
                    else:
                        tag_error = classify_error(exc, "s3:GetBucketTagging")
                        log.warning("s3 tag inspection failed", extra={"bucket": name, "code": tag_error.code})
                resources.append(
                    DiscoveredResource(
                        resource_type=RESOURCE_TYPE,
                        service="s3",
                        resource_id=name,
                        resource_name=name,
                        resource_arn=f"arn:{partition}:s3:::{name}",
                        region=bucket.get("BucketRegion", region),
                        discovered_at=now,
                        tags=tags,
                        tag_source="s3:GetBucketTagging",
                        tag_error=tag_error,
                    )
                )
    except AWS_ERRORS as exc:
        errors.append(ServiceError(service="s3", region=region, error=classify_error(exc, "s3:ListAllMyBuckets")))
    return resources, errors
