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

The example limits access to items whose partition key starts with `POLICY#` (via `dynamodb:LeadingKeys`). In the template, these operations now belong to `CloudGuardGovernanceAdmin`; see "Governance store identities (H2)" below.

**Not yet verified:**
- Neither the template's policies nor this example has been deployed or attached.
- The `LeadingKeys` condition hasn't been tested against real DynamoDB.

## Governance store identities (H2)
`infra/cloudguard-core.yaml` defines three identities, and none of them is deployed.

| Identity | What it is | Who uses it |
|---|---|---|
| `CloudGuardInspectorRole` | IAM role | Read-only discovery, tag reads, planned Bedrock explanations. **No governance-table access, no tagging.** |
| `CloudGuardGovernanceParticipant` | Managed policy, **unattached** | Attach it to the principals that create, approve, reject, revoke, abort and execute proposals. The CLI writes with the caller's own credentials, so separation of duties can compare verified identities. |
| `CloudGuardGovernanceAdmin` | Managed policy, **unattached** | Attach it to the allow-list and tagging-policy administrator(s) only. |
| `CloudGuardTaggerRole` | **Not in the template** | The remediation writer. It's designed in `infra/iam/proposed/`; no live writer exists. |

### Every DynamoDB call site and the IAM actions it needs
There is **no `dynamodb:TransactWriteItems` IAM action**. Each `Put`/`Update`/`Delete`/`Get` entry in a transaction is authorized as `PutItem`/`UpdateItem`/`DeleteItem`/`GetItem`, and `ConditionCheck` entries as `dynamodb:ConditionCheckItem` ([transaction-apis-iam](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html)).

| Call site | Request | IAM actions | Partition prefixes | Allowed by |
|---|---|---|---|---|
| `DynamoPolicyStore.commit`, `DynamoAuthzStore.commit` | transaction: `Put` VERSION + `Put` ACTIVE | `PutItem` | `POLICY#`, `AUTHZ#` | Admin A1 |
| `create` | transaction: 4 × `Put` | `PutItem` | `PROPOSAL#`, `IDEMP#` | Participant P1 |
| `append_transition`, `append_execution_event`, `finish_execution` | transaction: `Update` + `Put` | `UpdateItem`, `PutItem` | `PROPOSAL#` | P1 |
| `record_decision`, `start_execution`, `record_abort` | transaction: `Update` + `Put`s | `UpdateItem`, `PutItem` | `PROPOSAL#`, `NONCE#` (decision, abort), `IDEMP#` | P1 |
| `take_over_lease` | plain `update_item` | `UpdateItem` | `PROPOSAL#` | P2 |
| `repair_head` | plain `put_item` | `PutItem` | `PROPOSAL#` | P2 |
| all item reads | `get_item` | `GetItem` | all five | P3 (participant), A2 (admin: `AUTHZ#`, `POLICY#`) |
| proposal history and execution reads | `Query` | `Query` | `PROPOSAL#` | P4 |
| policy and allow-list history and show | `Query` | `Query` | `AUTHZ#`, `POLICY#` | A2 |

The code uses **no** `Delete` or `ConditionCheck` transaction entries, `TransactGetItems`, `BatchWriteItem`, `BatchGetItem`, `Scan`, PartiQL or secondary indexes. So `DeleteItem`, `ConditionCheckItem` and `BatchWriteItem` are granted to nobody, and every resource is the table ARN only. `backend/tests/test_iam_boundaries.py` re-checks all of this against the source code.

**Admins don't need `UpdateItem`:** both stores commit with two `Put`s.

**`--table` and the default policy:** with `--table`, the proposal flow only *reads* policies; it never bootstraps a default. So participants never write `POLICY#`.

### The conditions (types from AWS's Service Authorization Reference data, checked on 2026-10-16)
- **`dynamodb:LeadingKeys` (`ArrayOfString`, multivalued):**
  - `ForAllValues:StringLike` against the allowed prefixes: every partition key a request touches must match one of them. AWS requires `ForAllValues` with this key.
  - `Null: {"dynamodb:LeadingKeys": "false"}`: the key must be present. Without it, `ForAllValues` would also match a request that has no partition key at all.
- **`dynamodb:EnclosingOperation` (`String`, single-valued):**
  - `StringEquals: TransactWriteItems` (P1, A1) allows a write only as an entry of a transaction.
  - `Null: "true"` (P2) allows only plain, non-transactional writes.
  - The two can't overlap: a plain write lacks the key, so `StringEquals` fails; a transactional entry has it, so `Null: true` fails.
  - **This constrains the API path only. It doesn't enforce a transaction's conditions or which items it contains.**
- **`Query` doesn't support `EnclosingOperation`,** so no `Query` statement uses it.
- **Mixed-prefix transactions:** a transaction that puts an `AUTHZ#` item together with participant items is refused. That holds whether `LeadingKeys` is evaluated per entry or over the whole request; which of the two AWS actually does is **not yet verified**.

### Known limitations (participant scope)
- **Not bound to the caller's own proposals.** The participant policy grants **every** `PROPOSAL#*`, `IDEMP#*` and `NONCE#*` partition, and every sort key inside them. It does **not** limit a participant to proposals they own or are assigned to. With direct table access, any participant can create or overwrite any proposal's META, HEAD, EVENT, DECISION or EXEC items.
- **Idempotency and nonce records are shared namespaces.**
  - Their partition keys are `IDEMP#<scope>#<client key>` (`repository._idem_key`; the scope is one of `tagging-proposal-create`, `proposal-decision`, `proposal-execute`) and `NONCE#<client nonce>`. Neither contains a principal or proposal component.
  - So any participant can write another participant's idempotency or nonce records. For example, pointing an idempotency key at a different proposal or decision, pre-claiming keys (denial of service), or overwriting a nonce record.
  - Through the CLI, these writes are conditional (`attribute_not_exists`), so they can't overwrite; the risk is direct table access.
- **Deletion via TTL: closed by disabling TTL (Option D).**
  - Participants and administrators can write arbitrary attributes. With TTL enabled, they could set `ttl_epoch` in the past and have DynamoDB delete items they can't delete directly, including audit records and, for administrators, every `AUTHZ#` item (which would reopen the bootstrap).
  - The governance table therefore has **no TTL**. **Re-enabling TTL reopens this vulnerability.** No policy grants `dynamodb:UpdateTimeToLive`, and tests assert both points.
  - A `dynamodb:Attributes` allow-list was rejected. It depends on unverified per-entry evaluation inside mixed-prefix transactions, a deny-style variant could be bypassed under whole-request evaluation, it is brittle, and it wouldn't stop overwrites anyway.
- **Overwrite and forgery remain possible.**
  - IAM can't require a write to carry a condition (`attribute_not_exists`, sequence checks).
  - So a participant with direct table access can replace any `PROPOSAL#`, `IDEMP#` or `NONCE#` item, and an administrator any `AUTHZ#` or `POLICY#` item, with arbitrary content.
  - Point-in-time recovery (35 days) allows restoring. Detection would need DynamoDB Streams or CloudTrail data events, neither configured.
- **IAM does not enforce the approval workflow** against direct table access. The audit checks detect inconsistent records, but the hash chain isn't signed, so consistent forgeries can't be ruled out. Enforcement needs governance writes mediated by a server (Phase 7) or signed records.
- **Separation of duties between humans isn't enforced by these policies.** Nothing stops one person from holding a principal with the admin policy and another with the participant policy (or both on one principal). That is controlled by the account's IAM administrators: attachment, role trust, IAM Identity Center groups. Anyone with IAM-admin rights, or the root user, can bypass it. See "People and separation of duties (M3)" in [authorization.md](authorization.md#people-and-separation-of-duties-m3).

**Not verified (deployment blockers):**
- per-entry `LeadingKeys` and `EnclosingOperation` evaluation inside real transactions;
- `aws:PrincipalAccount` in the inspector trust policy;
- CloudFormation's `AllowedPattern` behaviour on a `CommaDelimitedList`;
- IAM Access Analyzer results;
- deployment.

Offline tests check the documents' structure only; they don't prove IAM runtime behaviour.

## Resource governance: approved tagging (planned)
`lambda:TagResource`, `dynamodb:TagResource`, `s3:PutBucketTagging` (plus `s3:GetBucketTagging` to preserve existing tags) and `ec2:CreateTags`.
- These belong only to the proposed `CloudGuardTaggerRole` (`infra/iam/proposed/`), which is not deployed.
- The inspector explicitly denies them, plus the untag actions and `tag:TagResources`/`tag:UntagResources`.
- **Coverage:** that deny covers CloudGuard's four resource types and the Resource Groups Tagging API, not every tagging API in AWS.

## Cost intelligence (Task 1, read-only)
Example policy: [`infra/iam/cost-intelligence-policy.json`](../infra/iam/cost-intelligence-policy.json): `ce:GetCostAndUsage`, `ce:GetAnomalyMonitors` and `ce:GetAnomalies`. The details, the pre-flight checklist and the open verification items are in [cost-intelligence.md](cost-intelligence.md). **Not yet attached or tested against AWS.**
