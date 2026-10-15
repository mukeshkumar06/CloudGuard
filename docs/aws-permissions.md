# AWS permissions

CloudGuard separates **read-only discovery** from **approval-gated writes**. Only discovery is implemented so far.

## Resource governance: discovery (read-only)

Example policy: [`infra/iam/governance-discovery-policy.json`](../infra/iam/governance-discovery-policy.json)

| API call (boto3) | IAM action | Resource scope | Why |
|---|---|---|---|
| `sts.get_caller_identity` | none needed | — | Identifies the real authenticated account. AWS requires no permission for this call. |
| `lambda.list_functions` | `lambda:ListFunctions` | `*` (no resource-level support) | List functions, paginated |
| `lambda.list_tags` | `lambda:ListTags` | function ARN | Tags aren't returned by ListFunctions |
| `dynamodb.list_tables` | `dynamodb:ListTables` | `*` | List tables, paginated |
| `dynamodb.describe_table` | `dynamodb:DescribeTable` | table ARN | Authoritative table ARN |
| `dynamodb.list_tags_of_resource` | `dynamodb:ListTagsOfResource` | table ARN | Tags, paginated |
| `s3.list_buckets` (with `BucketRegion`) | `s3:ListAllMyBuckets` | `*` | List buckets in the scanned region, paginated |
| `s3.get_bucket_location` | `s3:GetBucketLocation` | bucket ARN | Fallback when ListBuckets omits `BucketRegion` |
| `s3.get_bucket_tagging` | `s3:GetBucketTagging` | bucket ARN | Tags. `NoSuchTagSet` means "no tags", not a failure. |
| `ec2.describe_instances` | `ec2:DescribeInstances` | `*` (no resource-level support) | Instances with inline tags, paginated |

The example policy uses `"Resource": "*"` because it's a read-only, account-wide inventory policy. If you need tighter scoping, the actions marked with an ARN above can be limited to specific resources.

**Verification status:**
- **Not yet verified:** this example policy hasn't been attached to any principal or tested on its own.
- **What was tested:** live discovery ran with a developer IAM identity whose permissions are broader than this policy.
- **Where the scopes come from:** the resource scopes above were written from knowledge of AWS, not freshly checked. Re-check them against the [AWS Service Authorization Reference](https://docs.aws.amazon.com/service-authorization/latest/reference/reference_policies_actions-resources-contextkeys.html) before relying on them.

### Failure behaviour
- **Missing permission on a list call:** the whole service is reported in `service_errors`. Its resources may be absent, and the scan says so instead of hiding it.
- **Missing permission on a per-resource tag call:** that resource is `INSPECTION_FAILED` with the AWS error code. It is never classed as untagged or non-compliant.
- **Bucket with an unknown region:** sometimes `ListBuckets` omits `BucketRegion` *and* `GetBucketLocation` also fails. That bucket is then reported once as `INSPECTION_FAILED` with region `unresolved`. Live discovery de-duplicates resources by ARN across regional passes, so a multi-region scan counts it only once.
- **Not exercised against real AWS:**
  - The `GetBucketLocation` fallback. The live account always returned `BucketRegion`.
  - EC2 discovery, because there were no instances. Both are covered only by stubbed unit tests.
- **Throttling and 5xx:** botocore retries in standard mode (5 attempts, exponential backoff). If retries run out, the error becomes an inspection or service error.

## Policy store (DynamoDB, single table)

Example policy: [`infra/iam/governance-policy-store-policy.json`](../infra/iam/governance-policy-store-policy.json)

| Operation | IAM action | Notes |
|---|---|---|
| Read the active pointer or one version (`GetItem`) | `dynamodb:GetItem` | |
| List history (`Query`) | `dynamodb:Query` | |
| Save a version (`TransactWriteItems` with two `Put`s) | `dynamodb:PutItem` | Each `Put` inside a transaction is authorized as `PutItem` |

There is **no `dynamodb:TransactWriteItems` IAM action**. AWS authorizes each transaction entry against its underlying operation:
- `Put`, `Update`, `Delete` and `Get` entries use `PutItem`, `UpdateItem`, `DeleteItem` and `GetItem`.
- Only standalone `ConditionCheck` entries need `dynamodb:ConditionCheckItem`. CloudGuard doesn't use them: the condition expressions sit on the `Put`s themselves.
- Source: [Using IAM with DynamoDB transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html), checked on 2026-10-14.

The example limits access to items whose partition key starts with `POLICY#` (via `dynamodb:LeadingKeys`).

**Not yet verified:**
- The role in `cloudguard-core.yaml` already grants `GetItem`, `PutItem` and `Query` on the table, but neither the role nor this example has been deployed or attached.
- The `LeadingKeys` condition hasn't been tested against real DynamoDB.

## Resource governance: approved tagging (planned, Phase 4)
`lambda:TagResource`, `dynamodb:TagResource`, `s3:PutBucketTagging` (plus `s3:GetBucketTagging` to preserve existing tags), `ec2:CreateTags`. These will live in a separate role that is only assumed after approval.

## Cost intelligence (Task 1, read-only)
Example policy: [`infra/iam/cost-intelligence-policy.json`](../infra/iam/cost-intelligence-policy.json): `ce:GetCostAndUsage`, `ce:GetAnomalyMonitors` and `ce:GetAnomalies`. The details, the pre-flight checklist and the open verification items are in [cost-intelligence.md](cost-intelligence.md). **Not yet attached or tested against AWS.**
