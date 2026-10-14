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
  - **Two trust statements:** `sts:AssumeRole` is conditioned on the session name, and `sts:SetSourceIdentity` on the source identity. Keeping them separate avoids relying on `sts:RoleSessionName` being present in the `SetSourceIdentity` request context, which isn't documented.
  - **The backend's own permissions:** the backend role also needs `sts:AssumeRole` on the tagger role in its identity policy. That isn't part of these documents.

### 3.2 Permission ceiling on the tagger role
See [`proposed/tagger-role-policy.json`](../infra/iam/proposed/tagger-role-policy.json).

**Allowed:**
- **Lambda and DynamoDB:** `TagResource` on function and table ARNs in this account only.
- **EC2:** `ec2:CreateTags` on **instance** ARNs only, with `Null ec2:CreateAction` so it covers existing instances only (F5).
- **S3:** `s3:GetBucketTagging` and `s3:PutBucketTagging` on buckets owned by this account (`s3:ResourceAccount`).
- **Verification reads** for the post-write check.

**Explicitly denied:**
- `UntagResource` and `DeleteTags`: CloudGuard never removes tags.
- Writing **protected keys**: `cloudguard:protected` plus a deployment-supplied list of keys used for ABAC (F8), using `ForAnyValue:StringEqualsIgnoreCase`.
- Writing `cloudguard:`-prefixed keys, using `StringLike` with the four common spellings (see the case-sensitivity note below).
- Any tagging of resources tagged `cloudguard:protected=true`, using `StringEqualsIgnoreCase` on the value. This deny also covers S3 via `aws:ResourceTag`.

**Case sensitivity (verified in the IAM docs on 2026-10-14):**
- *"condition key names are not case-sensitive"*: `aws:ResourceTag/Owner` also matches a tag named `owner`.
- `StringLike` and `StringEquals` compare *values*, including the key list in `aws:TagKeys`, case-sensitively.

So a deny on `aws:TagKeys` written with `StringLike team*` would miss a request for `TEAM` that someone else's ABAC policy, keyed on `aws:ResourceTag/team`, still honours. The design handles this in three ways:
- **Exact protected keys** use `StringEqualsIgnoreCase`, which closes the gap for exact keys.
- **Prefix denies** can't be made case-insensitive, because IAM has no `StringLikeIgnoreCase`. The `cloudguard:` prefix deny lists common spellings only. The **application** rejects `cloudguard:` keys case-insensitively (`validation.RESERVED_PREFIX`), and the session policy only allows exactly approved keys.
- **ABAC keys** must therefore be listed **exactly** in `<PROTECTED_TAG_KEYS>`, not as prefixes.

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
- **The session policy is voluntary.** The caller chooses it; the trust policy can't force one. A compromised backend could assume the tagger role *without* a session policy and get the full ceiling of §3.2. The per-approval scoping therefore protects against bugs and over-broad requests, not against a compromised backend. The ceiling and its denies are the actual boundary, so keep them as narrow as practical.

### 3.4 S3 compensating controls (IAM can't restrict keys)
1. **Read, merge, write:**
   - `GetBucketTagging` the current set.
   - Verify it still matches the proposal's `current_tags` exactly.
   - Add the approved keys only.
   - `PutBucketTagging` the merged set.
   - Never send a set that drops an existing key: the application asserts `old ⊆ new`.
2. **Narrow window:** S3 has no conditional or versioned tag write, so a concurrent change between GET and PUT can't be fully prevented. Keep the window minimal, re-read after the write, and report any drift as a failed execution.
3. **Scope:** exact bucket ARNs via the session policy; the protected-resource deny from §3.2 still applies.
4. **Residual risk:** `s3:PutBucketTagging` also authorizes `DeleteBucketTagging`, and `PutBucketTagging` overwrites the entire tag set. Both are verified in the S3 API reference. Within its scope, a session can therefore remove every tag from an approved bucket, and the ceiling allows that for any unprotected bucket in the account. IAM can't prevent it: only the application's merge rule and post-write verification can.

### 3.5 What the application still enforces
These hold even where IAM can't enforce them:
- No `aws:` keys.
- AWS tag constraints.
- Policy-value validation.
- No overwrite without an `OVERWRITE` change and explicit confirmation.
- No tag removal.

IAM is the second line of defence, not the only one.

## 4. Verification status
**Verified from AWS documentation (not by execution):**
- The condition-key support matrix in §2.
- `DeleteBucketTagging` requires `s3:PutBucketTagging`, and `PutBucketTagging` overwrites existing tags.
- Tag-key names in condition keys are case-insensitive, while `StringLike`/`StringEquals` values are case-sensitive.
- No `StringLikeIgnoreCase` operator exists.

**Syntactically valid JSON only.** Being well-formed doesn't make a policy correct. Each item below still needs **IAM Access Analyzer `ValidatePolicy`** and/or **live testing in a sandbox account**:
1. None of these policies has been validated by Access Analyzer, simulated with the IAM policy simulator, or deployed.
2. Session policy plus role intersection: that `ForAllValues:StringEquals aws:TagKeys` plus `StringEquals aws:RequestTag/<key>` restricts Lambda `TagResource`, DynamoDB `TagResource` and EC2 `CreateTags` exactly as described.
3. That `Null ec2:CreateAction = true` allows tagging existing instances and blocks tag-on-launch.
4. That `s3:ResourceAccount` evaluates as expected for `GetBucketTagging`/`PutBucketTagging`.
5. That `ForAnyValue:StringEqualsIgnoreCase` on `aws:TagKeys` denies case variants for all three services.
6. That `aws:ResourceTag/cloudguard:protected` with `StringEqualsIgnoreCase` matches case variants of both key and value, including for S3.
7. Trust policy: AssumeRole with session-name and source-identity conditions, split across two statements.
8. Resource wildcards:
   - `table/*` also matches DynamoDB stream ARNs (`table/<t>/stream/...`), which `dynamodb:TagResource` supports. The ceiling therefore also permits tagging streams; the session policy's exact ARNs narrow it.
   - `function:*` also matches qualified ARNs.
9. That an empty `aws:TagKeys` set under `ForAllValues` (vacuously true) can't be exploited. A `TagResource` call with no tags changes nothing.

**Placeholders:** `<ACCOUNT_ID>`, `<CLOUDGUARD_BACKEND_ROLE>` and `<PROTECTED_TAG_KEYS>` must be filled at deployment. The protected key list must come from the organisation's actual ABAC policies, as exact keys.

## 5. Phase 4 migration steps (each requires approval)
1. Add `CloudGuardTaggerRole` (trust plus ceiling policy) to the template, as parameters.
2. Remove `GovernanceTaggingOnly`, `S3BucketTagging` and `tag:TagResources` from the inspector role.
3. Implement session-policy generation from `TaggingProposal.approval_binding()`, with tests for size and chunking.
4. Validate the policies with IAM Access Analyzer before any deployment.
