"""AWS Lambda function discovery. ListFunctions does not return tags, so each function needs ListTags."""

from __future__ import annotations

import logging
from datetime import datetime

from cloudguard.governance.discovery.common import AWS_ERRORS, classify_error
from cloudguard.governance.models import DiscoveredResource, ServiceError

log = logging.getLogger(__name__)

RESOURCE_TYPE = "AWS::Lambda::Function"


def discover_lambda(client, region: str, now: datetime) -> tuple[list[DiscoveredResource], list[ServiceError]]:
    resources: list[DiscoveredResource] = []
    errors: list[ServiceError] = []
    try:
        for page in client.get_paginator("list_functions").paginate():
            for fn in page.get("Functions", []):
                arn = fn["FunctionArn"]
                tags, tag_error = None, None
                try:
                    tags = dict(client.list_tags(Resource=arn).get("Tags", {}))
                except AWS_ERRORS as exc:
                    tag_error = classify_error(exc, "lambda:ListTags")
                    log.warning("lambda tag inspection failed", extra={"arn": arn, "code": tag_error.code})
                resources.append(
                    DiscoveredResource(
                        resource_type=RESOURCE_TYPE,
                        service="lambda",
                        resource_id=fn["FunctionName"],
                        resource_name=fn["FunctionName"],
                        resource_arn=arn,
                        region=region,
                        discovered_at=now,
                        tags=tags,
                        tag_source="lambda:ListTags",
                        tag_error=tag_error,
                    )
                )
    except AWS_ERRORS as exc:
        errors.append(ServiceError(service="lambda", region=region, error=classify_error(exc, "lambda:ListFunctions")))
    return resources, errors
