"""Amazon DynamoDB table discovery: ListTables -> DescribeTable (for the ARN) -> ListTagsOfResource."""

from __future__ import annotations

import logging
from datetime import datetime

from cloudguard.governance.discovery.common import AWS_ERRORS, classify_error
from cloudguard.governance.models import DiscoveredResource, InspectionError, ServiceError

log = logging.getLogger(__name__)

RESOURCE_TYPE = "AWS::DynamoDB::Table"


def _list_all_tags(client, arn: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for page in client.get_paginator("list_tags_of_resource").paginate(ResourceArn=arn):
        tags.update({t["Key"]: t["Value"] for t in page.get("Tags", [])})
    return tags


def discover_dynamodb(
    client, region: str, account_id: str, partition: str, now: datetime
) -> tuple[list[DiscoveredResource], list[ServiceError]]:
    resources: list[DiscoveredResource] = []
    errors: list[ServiceError] = []
    try:
        for page in client.get_paginator("list_tables").paginate():
            for name in page.get("TableNames", []):
                arn = f"arn:{partition}:dynamodb:{region}:{account_id}:table/{name}"
                tags: dict[str, str] | None = None
                tag_error: InspectionError | None = None
                op = "dynamodb:DescribeTable"
                try:
                    arn = client.describe_table(TableName=name)["Table"]["TableArn"]
                    op = "dynamodb:ListTagsOfResource"
                    tags = _list_all_tags(client, arn)
                except AWS_ERRORS as exc:
                    tag_error = classify_error(exc, op)
                    log.warning("dynamodb tag inspection failed", extra={"table": name, "code": tag_error.code})
                resources.append(
                    DiscoveredResource(
                        resource_type=RESOURCE_TYPE,
                        service="dynamodb",
                        resource_id=name,
                        resource_name=name,
                        resource_arn=arn,
                        region=region,
                        discovered_at=now,
                        tags=tags,
                        tag_source="dynamodb:ListTagsOfResource",
                        tag_error=tag_error,
                    )
                )
    except AWS_ERRORS as exc:
        errors.append(ServiceError(service="dynamodb", region=region, error=classify_error(exc, "dynamodb:ListTables")))
    return resources, errors
