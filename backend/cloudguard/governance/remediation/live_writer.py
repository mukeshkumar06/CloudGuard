"""Live tag reading and writing for an APPROVED execution (Checkpoint 5). Never used before approval.

LiveTagAccount reads a resource's tags and identity with the read-only discovery APIs, and LiveWriter
writes approved tags with each service's own tagging API:

    Lambda function   lambda:ListTags / lambda:TagResource (Resource=function ARN, Tags=map)
    DynamoDB table    dynamodb:DescribeTable + ListTagsOfResource / dynamodb:TagResource (list of Key/Value)
    EC2 instance      ec2:DescribeInstances / ec2:CreateTags (Resources=[instance id], list of Key/Value)
    S3 bucket         s3:GetBucketTagging + ListBuckets / s3:PutBucketTagging

Every write adds or overwrites only the approved keys; no key is ever removed. Lambda, DynamoDB and EC2
tagging APIs merge by themselves. S3's PutBucketTagging REPLACES the whole tag set, so the writer reads
the current set immediately before writing, merges the approved keys in and writes the merged set. A tag
another actor adds between that read and the write would be lost: S3 offers no conditional tagging. The
executor's read-back verification reports unrelated tags that changed during execution, but it cannot
prevent this window (documented in docs/live-tagging.md).

The executor (executor.py) is the only caller: it builds every ApprovedWrite from the stored, approved
proposal after verifying the approval, re-reads each resource immediately before writing, records a
fenced WRITE_INTENT, and reads every resource back afterwards. Errors map onto the executor's existing
categories (throttled and transient are retried; access denied and not found are not).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from cloudguard.governance.custom_tagging.models import ChangeAction
from cloudguard.governance.discovery.common import _ACCESS_DENIED, _NOT_FOUND, _THROTTLED, _UNAVAILABLE
from cloudguard.governance.remediation.writers import (
    ApprovedWrite,
    FixtureAccessDenied,
    FixtureError,
    FixtureNotFound,
    FixtureThrottled,
    FixtureTransient,
    ReadPurpose,
    ResourceSnapshot,
    WriteResult,
)

if TYPE_CHECKING:
    import boto3

# Explicit timeouts and few retries: the executor retries throttling itself, within its write budget.
LIVE_CONFIG = Config(retries={"mode": "standard", "max_attempts": 1}, connect_timeout=3, read_timeout=10,
                     user_agent_extra="cloudguard-ai-tagger")

SUPPORTED_TYPES = ("AWS::Lambda::Function", "AWS::DynamoDB::Table", "AWS::EC2::Instance", "AWS::S3::Bucket")


# The executor's error classes are named for the fixture, where they were introduced; live errors subclass them so
# the executor's retry and failure handling is the same for both.
class LiveThrottled(FixtureThrottled):
    pass


class LiveTransient(FixtureTransient):
    pass


class LiveAccessDenied(FixtureAccessDenied):
    pass


class LiveNotFound(FixtureNotFound):
    pass


class LiveUnsupported(FixtureError):
    category = "INELIGIBLE"


def _classify(exc: Exception, what: str) -> FixtureError:
    """Map a botocore error onto the executor's categories. Messages carry the AWS code and operation, never
    credentials (AWS error text can name the caller, so only the code is kept)."""
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", "Unknown"))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        message = f"{code} from {what}"
        if code in _THROTTLED:
            return LiveThrottled(message)
        if code in _ACCESS_DENIED:
            return LiveAccessDenied(message)
        if code in _NOT_FOUND or code in ("NoSuchEntity", "InvalidInstanceID.Malformed"):
            return LiveNotFound(message)
        if code in _UNAVAILABLE or status >= 500:
            return LiveTransient(message)
        return FixtureError(message)
    if isinstance(exc, EndpointConnectionError | BotoCoreError):
        return LiveTransient(f"{type(exc).__name__} from {what}")
    return FixtureError(f"{type(exc).__name__} from {what}")


def parse_arn(arn: str) -> tuple[str, str, str, str]:
    """(resource_type, region, account_id, resource_id) for the four supported ARN shapes."""
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        raise LiveUnsupported(f"not an ARN: {arn}")
    _, _partition, service, region, account, rest = parts
    if service == "lambda" and rest.startswith("function:"):
        return "AWS::Lambda::Function", region, account, rest.split(":")[1]
    if service == "dynamodb" and rest.startswith("table/"):
        return "AWS::DynamoDB::Table", region, account, rest.split("/")[1]
    if service == "ec2" and rest.startswith("instance/"):
        return "AWS::EC2::Instance", region, account, rest.split("/")[1]
    if service == "s3" and not region and not account and rest and "/" not in rest:
        return "AWS::S3::Bucket", "", "", rest
    raise LiveUnsupported(f"tagging is supported for Lambda functions, DynamoDB tables, EC2 instances and S3 "
                          f"buckets only: {arn}")


def _request_id(response: dict) -> str:
    return str(response.get("ResponseMetadata", {}).get("RequestId", ""))


class LiveTagAccount:
    """The authenticated AWS account, as the executor sees it: read(arn) and (through LiveWriter) write."""

    mode = "live"

    def __init__(self, session: boto3.Session, account_id: str,
                 client_factory: Callable[[str, str], object] | None = None):
        self.account_id = account_id
        self._session = session
        self._clients: dict[tuple[str, str], object] = {}
        self._factory = client_factory or (lambda service, region: session.client(
            service, region_name=region, config=LIVE_CONFIG))

    def client(self, service: str, region: str):
        key = (service, region)
        if key not in self._clients:
            self._clients[key] = self._factory(service, region)
        return self._clients[key]

    # ---------------------------------------------------------------- reads

    def read(self, arn: str, purpose: ReadPurpose, stale_tags: dict[str, str] | None = None) -> ResourceSnapshot:
        resource_type, region, account, rid = parse_arn(arn)
        if account and account != self.account_id:
            raise LiveAccessDenied(f"{arn} is not in account {self.account_id}")
        try:
            if resource_type == "AWS::Lambda::Function":
                return self._read_lambda(arn, region, rid)
            if resource_type == "AWS::DynamoDB::Table":
                return self._read_dynamodb(arn, region, rid)
            if resource_type == "AWS::EC2::Instance":
                return self._read_ec2(arn, region, rid)
            return self._read_s3(arn, rid)
        except FixtureError:
            raise
        except (ClientError, BotoCoreError) as exc:
            err = _classify(exc, f"{resource_type} read")
            if isinstance(err, LiveNotFound):
                return ResourceSnapshot(exists=False, arn=arn)
            raise err from None

    def _snapshot(self, arn, resource_type, region, uid, uid_kind, tags, request_id) -> ResourceSnapshot:
        return ResourceSnapshot(exists=True, arn=arn, resource_type=resource_type, account_id=self.account_id,
                                region=region, uid=uid, uid_kind=uid_kind, tags=tags, request_id=request_id)

    def _read_lambda(self, arn, region, name):
        resp = self.client("lambda", region).list_tags(Resource=arn)
        return self._snapshot(arn, "AWS::Lambda::Function", region, None, None, dict(resp.get("Tags", {})),
                              _request_id(resp))

    def _read_dynamodb(self, arn, region, name):
        c = self.client("dynamodb", region)
        table = c.describe_table(TableName=name)["Table"]
        tags, token, request_id = {}, None, ""
        while True:
            resp = c.list_tags_of_resource(ResourceArn=arn, **({"NextToken": token} if token else {}))
            request_id = _request_id(resp)
            tags.update({t["Key"]: t["Value"] for t in resp.get("Tags", [])})
            token = resp.get("NextToken")
            if not token:
                break
        return self._snapshot(arn, "AWS::DynamoDB::Table", region, table.get("TableId"), "TableId", tags, request_id)

    def _read_ec2(self, arn, region, instance_id):
        resp = self.client("ec2", region).describe_instances(InstanceIds=[instance_id])
        instances = [i for r in resp.get("Reservations", []) for i in r.get("Instances", [])]
        live = [i for i in instances if i.get("State", {}).get("Name") not in ("terminated", "shutting-down")]
        if not live:
            return ResourceSnapshot(exists=False, arn=arn, request_id=_request_id(resp))
        tags = {t["Key"]: t["Value"] for t in live[0].get("Tags", [])}
        return self._snapshot(arn, "AWS::EC2::Instance", region, live[0]["InstanceId"], "InstanceId", tags,
                              _request_id(resp))

    def _bucket_region(self, bucket: str) -> str:
        constraint = self.client("s3", "us-east-1").get_bucket_location(Bucket=bucket).get("LocationConstraint")
        return {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(constraint, constraint)

    def _bucket_tags(self, bucket: str, region: str) -> tuple[dict[str, str], str]:
        try:
            resp = self.client("s3", region).get_bucket_tagging(Bucket=bucket)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "NoSuchTagSet":
                return {}, _request_id(exc.response)
            raise
        return {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}, _request_id(resp)

    def _read_s3(self, arn, bucket):
        region = self._bucket_region(bucket)
        listing = self.client("s3", "us-east-1").list_buckets(Prefix=bucket)
        created = next((b["CreationDate"] for b in listing.get("Buckets", []) if b["Name"] == bucket), None)
        if created is None:
            return ResourceSnapshot(exists=False, arn=arn, request_id=_request_id(listing))
        tags, request_id = self._bucket_tags(bucket, region)
        uid = created.isoformat() if hasattr(created, "isoformat") else str(created)
        return self._snapshot(arn, "AWS::S3::Bucket", region, uid, "CreationDate", tags, request_id)

    def identities(self, arns: list[str]) -> dict[str, str]:
        """Immutable identities at proposal time, so a deleted and recreated resource is never tagged by an
        approval for the old one (Lambda has none)."""
        out = {}
        for arn in arns:
            snap = self.read(arn, ReadPurpose.REVALIDATE)
            if snap.exists and snap.uid:
                out[arn] = snap.uid
        return out

    # ---------------------------------------------------------------- writes (LiveWriter only)

    def _apply_tags(self, arn: str, tags: dict[str, str]) -> WriteResult:
        resource_type, region, _account, rid = parse_arn(arn)
        pairs = [{"Key": k, "Value": v} for k, v in sorted(tags.items())]
        try:
            if resource_type == "AWS::Lambda::Function":
                resp = self.client("lambda", region).tag_resource(Resource=arn, Tags=dict(tags))
            elif resource_type == "AWS::DynamoDB::Table":
                resp = self.client("dynamodb", region).tag_resource(ResourceArn=arn, Tags=pairs)
            elif resource_type == "AWS::EC2::Instance":
                resp = self.client("ec2", region).create_tags(Resources=[rid], Tags=pairs)
            else:
                bucket_region = self._bucket_region(rid)
                current, _ = self._bucket_tags(rid, bucket_region)
                merged = {**current, **tags}  # PutBucketTagging replaces the set: keep every existing key
                if len(merged) > 50:
                    raise LiveUnsupported(f"{arn} would have {len(merged)} tags; S3 allows 50")
                resp = self.client("s3", bucket_region).put_bucket_tagging(
                    Bucket=rid, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in sorted(merged.items())]})
        except FixtureError:
            raise
        except (ClientError, BotoCoreError) as exc:
            raise _classify(exc, f"{resource_type} tag write") from None
        return WriteResult(arn=arn, written_keys=sorted(tags), request_id=_request_id(resp))


class LiveWriter:
    """Applies one approved write to the live account: only the approved keys, add or overwrite, never remove."""

    def __init__(self, account: LiveTagAccount):
        if type(account) is not LiveTagAccount:
            raise TypeError("LiveWriter only writes to a LiveTagAccount")
        self.account = account

    def apply(self, write: ApprovedWrite) -> WriteResult:
        if not isinstance(write, ApprovedWrite):
            raise TypeError("LiveWriter accepts only ApprovedWrite instances")
        for c in write.changes:
            if c.action not in (ChangeAction.ADD, ChangeAction.OVERWRITE):
                raise ValueError(f"unsupported change action {c.action}")
        return self.account._apply_tags(write.resource_arn, {c.key: c.new_value for c in write.changes})
