"""Amazon EC2 instance discovery. DescribeInstances returns tags inline, so no per-resource tag call.

Instances in the `shutting-down` and `terminated` states are excluded: they can't be meaningfully
governed and AWS removes them on its own (terminated instances stay visible for about an hour).
"""

from __future__ import annotations

from datetime import datetime

from cloudguard.governance.discovery.common import AWS_ERRORS, classify_error
from cloudguard.governance.models import DiscoveredResource, ServiceError

RESOURCE_TYPE = "AWS::EC2::Instance"

LIVE_STATES = ["pending", "running", "stopping", "stopped"]


def discover_ec2(
    client, region: str, account_id: str, partition: str, now: datetime
) -> tuple[list[DiscoveredResource], list[ServiceError]]:
    resources: list[DiscoveredResource] = []
    errors: list[ServiceError] = []
    try:
        pages = client.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": LIVE_STATES}]
        )
        for page in pages:
            for reservation in page.get("Reservations", []):
                owner = reservation.get("OwnerId") or account_id
                for inst in reservation.get("Instances", []):
                    instance_id = inst["InstanceId"]
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    name = next((v for k, v in tags.items() if k == "Name" and v.strip()), instance_id)
                    resources.append(
                        DiscoveredResource(
                            account_id=account_id,
                            resource_type=RESOURCE_TYPE,
                            service="ec2",
                            resource_id=instance_id,
                            resource_name=name,
                            resource_arn=f"arn:{partition}:ec2:{region}:{owner}:instance/{instance_id}",
                            region=region,
                            discovered_at=now,
                            tags=tags,
                            tag_source="ec2:DescribeInstances",
                        )
                    )
    except AWS_ERRORS as exc:
        errors.append(ServiceError(service="ec2", region=region, error=classify_error(exc, "ec2:DescribeInstances")))
    return resources, errors
