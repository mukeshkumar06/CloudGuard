# Phase 4 IAM design: approved tagging (proposed, not deployed)

Status: **design only.** Nothing here is deployed or attached. `infra/cloudguard-core.yaml` is unchanged apart from a comment pointing here. Its replacement is Phase 4 work and needs explicit approval.

## 1. Current template: findings

`CloudGuardInspectorRole` in `infra/cloudguard-core.yaml` currently grants:

```yaml
- Sid: GovernanceTaggingOnly
  Action: [tag:TagResources, lambda:TagResource, ec2:CreateTags, dynamodb:TagResource]
  Resource: "*"
  Condition: { ForAllValues:StringEquals: { aws:TagKeys: [Owner, Project, Environment, CostCenter] } }
- Sid: S3BucketTagging
  Action: [s3:PutBucketTagging]
  Resource: "*"
```

| # | Finding | Impact |
|---|---|---|
| F1 | **Key allow-list vs custom tags.** Only the four mandatory policy keys are allowed. | Approved custom tags such as `ServiceWRK=Lambda` (Phase 3 `CUSTOM` category) would be denied by AWS for Lambda, DynamoDB and EC2. The application and IAM disagree. |
| F2 | **S3 has no key restriction at all.** `s3:PutBucketTagging` supports no `aws:TagKeys`/`aws:RequestTag` condition keys (verified below), and the statement has no condition. | The "governance tags only" intent never applied to S3. |
| F3 | **`PutBucketTagging` replaces the whole tag set.** It also authorizes `DeleteBucketTagging`; there is no separate IAM action for that. | Any principal with this grant can **wipe or rewrite every tag** on any bucket. |
| F4 | **Read and write in one role.** The inspector also holds tagging writes. | Breaks the agreed separation between read-only inspection and approved writes. |
| F5 | **`ec2:CreateTags` on `*`.** | Allows tagging *any* EC2 resource type (security groups, volumes, ...), not just instances. |
| F6 | **`tag:TagResources` granted.** | Unneeded. CloudGuard uses service-specific APIs, and this broadens the surface. |
| F7 | **Trust policy is the account root.** | Any principal in the account allowed `sts:AssumeRole` can assume a role with write permissions. |
| F8 | **No guard against ABAC escalation.** | If other IAM policies grant access based on tags (`aws:ResourceTag/team`), the ability to write such a tag can grant access. |

**Simply removing the key allow-list would fix F1 but worsen F8.** The design below replaces it with tighter, per-approval scoping instead.

## 2. Verified condition-key support

Read from the AWS Service Authorization Reference on 2026-10-14.

| Action | Resource type | `aws:TagKeys` | `aws:RequestTag/${k}` | `aws:ResourceTag/${k}` | Other notable keys |
|---|---|---|---|---|---|
| `lambda:TagResource` | `function` | ✅ | ✅ | ✅ | — |
| `dynamodb:TagResource` | `table` | ✅ | ✅ | ✅ | — |
| `ec2:CreateTags` | `instance` | ✅ | ✅ | ✅ | `ec2:CreateAction`, `ec2:Region`, `ec2:ResourceTag/${k}` |
| `s3:PutBucketTagging` | `bucket` | ❌ | ❌ | ✅ | `s3:BucketTag/${k}`, `s3:ResourceAccount` |
| `lambda:UntagResource`, `dynamodb:UntagResource`, `ec2:DeleteTags` | as above | ✅ | — | ✅ | — |

Sources: [Lambda](https://docs.aws.amazon.com/service-authorization/latest/reference/list_lambda.html), [DynamoDB](https://docs.aws.amazon.com/service-authorization/latest/reference/list_dynamodb.html), [EC2](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html), [S3](https://docs.aws.amazon.com/service-authorization/latest/reference/list_s3.html).

## 3. Proposed design

### 3.1 Two roles
- **`CloudGuardInspectorRole`** is read-only. Remove all tagging statements (F4, F6).
- **`CloudGuardTaggerRole`** holds writes only. It is assumed **only at execution time, for one approved proposal**.
  - **Trust** ([`proposed/tagger-trust-policy.json`](../infra/iam/proposed/tagger-trust-policy.json)): only the CloudGuard backend role may assume it (F7).
  - **Session naming:** the trust policy requires a session name of `cg-proposal-*` and a `SourceIdentity` of `cloudguard:*`. CloudTrail then attributes every write to a proposal.

### 3.2 Permission ceiling on the tagger role
See [`proposed/tagger-role-policy.json`](../infra/iam/proposed/tagger-role-policy.json).

**Allowed:**
- **Lambda and DynamoDB:** `TagResource` on function and table ARNs in this account only.
- **EC2:** `ec2:CreateTags` on **instance** ARNs only, with `Null ec2:CreateAction` so it covers existing instances only (F5).
- **S3:** `s3:GetBucketTagging` and `s3:PutBucketTagging` on buckets owned by this account (`s3:ResourceAccount`).
- **Verification reads** for the post-write check.

**Explicitly denied:**
- `UntagResource` and `DeleteTags`: CloudGuard never removes tags.
- Writing **protected keys**: `cloudguard:*`, plus a deployment-supplied list of keys used for ABAC (F8).
- Any tagging of resources tagged `cloudguard:protected=true`. This deny also covers S3 via `aws:ResourceTag`.

**No key allow-list on the role.** Arbitrary *legitimate* custom keys are permitted at the ceiling (F1). The per-approval session policy below does the narrowing.

### 3.3 Per-approval session policy (the real scoping)
At execution, the backend calls `sts:AssumeRole` with an **inline session policy** generated from the approved proposal ([example](../infra/iam/proposed/tagger-session-policy.example.json)). The effective permissions are the *intersection* of the role policy and the session policy:
- **Resources:** exactly the ARNs that have `CHANGE_PROPOSED` records.
- **Lambda, DynamoDB and EC2:**
  - `ForAllValues:StringEquals aws:TagKeys` is limited to exactly the approved keys.
  - `StringEquals aws:RequestTag/<key>` is limited to exactly the approved values.
  - **The IAM layer therefore enforces the approved key *and* value**, not just the application.
- **S3:** exact bucket ARNs only. Key and value scoping isn't possible in IAM (F2), so see §3.4.
- **Credentials:** short-lived, with the minimum 15-minute `DurationSeconds`, and never returned to any model or client.
- **Size limit:** session policies are capped at 2,048 characters. Large bulk proposals are executed in **chunks**, one session per chunk, and every chunk is still a subset of the same approved proposal.

### 3.4 S3 compensating controls (IAM can't restrict keys)
1. **Read, merge, write:**
   - `GetBucketTagging` the current set.
   - Verify it still matches the proposal's `current_tags` exactly.
   - Add the approved keys only.
   - `PutBucketTagging` the merged set.
   - Never send a set that drops an existing key: the application asserts `old ⊆ new`.
2. **Narrow window:** S3 has no conditional or versioned tag write, so a concurrent change between GET and PUT can't be fully prevented. Keep the window minimal, re-read after the write, and report any drift as a failed execution.
3. **Scope:** exact bucket ARNs via the session policy; the protected-resource deny from §3.2 still applies.

### 3.5 What the application still enforces
These hold even where IAM can't enforce them:
- No `aws:` keys.
- AWS tag constraints.
- Policy-value validation.
- No overwrite without an `OVERWRITE` change and explicit confirmation.
- No tag removal.

IAM is the second line of defence, not the only one.

## 4. Not yet verified
- **Untested:** none of these policies has been deployed or simulated (IAM policy simulator / Access Analyzer `ValidatePolicy`).
- **Assumed behaviour:**
  - That `ForAllValues:StringEquals aws:TagKeys` plus `aws:RequestTag` in a session policy behaves as described for all three services.
  - The exact behaviour of `Null ec2:CreateAction` for tagging existing instances.
- **S3:** whether `s3:ResourceAccount` evaluates as expected for `PutBucketTagging`.
- **Placeholders:** `<ACCOUNT_ID>`, `<CLOUDGUARD_BACKEND_ROLE>` and `<PROTECTED_TAG_KEY_PATTERNS>` must be filled at deployment. The protected key list must come from the organisation's actual ABAC policies.

## 5. Phase 4 migration steps (each requires approval)
1. Add `CloudGuardTaggerRole` (trust plus ceiling policy) to the template, as parameters.
2. Remove `GovernanceTaggingOnly`, `S3BucketTagging` and `tag:TagResources` from the inspector role.
3. Implement session-policy generation from `TaggingProposal.approval_binding()`, with tests for size and chunking.
4. Validate the policies with IAM Access Analyzer before any deployment.
