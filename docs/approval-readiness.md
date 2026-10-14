# Proposal → approval readiness (for Phase 4)

Phase 3 produces proposals but implements **no approval or execution**. This document fixes the semantics Phase 4 must build on.

## Proposal identity and integrity
| Field | Semantics |
|---|---|
| `schema_version` | `cloudguard.tagging-proposal/v1`. Unknown versions are rejected. |
| `proposal_id` | `proposal-<uuid4>`. Unique per request, and never reused, even for identical text. |
| `content_hash` | SHA-256 over canonical JSON of **every other field**, including `approval_status`, records, impact, policy reference, timestamps and the request. It's verified on every load (`model_validate`). |
| `created_at` / `expires_at` | Default TTL is 15 minutes; it must be greater than 0 and at most 1 hour (validated). |
| `approval_status` | The **initial** state only: `PENDING` (approvable) or `NOT_APPROVABLE`. It's derived and hashed, so it can't be flipped. |
| `blocking_reasons` | Present exactly when the proposal is `NOT_APPROVABLE`: policy violations, and/or "no tag would change". |
| `policy` | `{policy_id, version, content_hash}` of the policy the proposal was evaluated against. |

**The hash is an integrity binding, not authentication.** Anyone holding a proposal body can recompute `content_hash`. For example, they could shift `created_at` and `expires_at` forward, and the validators would still pass. Therefore:
- **Store server-side:** Phase 4 must persist each proposal server-side when it's created, and approve only by `(proposal_id, content_hash)` looked up in that trusted store. It must never accept a client- or model-supplied proposal body.
- **Use time-aware approvability:** decide approvability with `approvable_at(now)`, which is false once `now >= expires_at` and always false for `NOT_APPROVABLE` proposals. The `approval_status` field records only the state at creation.

**Proposals are immutable.** Later states (APPROVED, REJECTED, EXPIRED, EXECUTING, EXECUTED, FAILED) belong in separate, append-only approval and audit records keyed by `(proposal_id, content_hash)`. They are never written back into the proposal.

**Validators already reject:**
- a forged or recomputed hash over inconsistent content;
- an impact summary that differs from what the records produce;
- an overwrite in an add operation;
- a wrong overwrite-confirmation flag;
- a specific proposal touching more than one resource;
- a record from another account;
- duplicate resources;
- an approval status that doesn't match approvability.

## What an authenticated approval must bind to
`TaggingProposal.approval_binding()` returns the exact data an approval covers:

```json
{
  "schema_version": "...", "proposal_id": "...", "content_hash": "sha256:...",
  "account_id": "...", "policy": {"policy_id": "...", "version": 1, "content_hash": "sha256:..."},
  "operation": "BULK_UPDATE_TAG", "expires_at": "...", "requires_overwrite_confirmation": true,
  "expected_state": [
    {"resource_arn": "...", "region": "...", "resource_type": "...",
     "key": "Owner", "expected_current_value": "payments-team", "new_value": "platform-team"}
  ]
}
```

Phase 4 must also record, outside the proposal:
- **Approver identity:** an authenticated principal (MCP auth in Phase 7). Free text or anything a model asserts never counts.
- **Approval timestamp,** plus a **single-use approval nonce** to prevent replay.
- **Overwrite acknowledgement:** a separate, explicit acknowledgement whenever `requires_overwrite_confirmation` is true.
- **Approve-all-or-nothing:** a subset of a bulk proposal can't be approved. A smaller change needs a new proposal.

## Revalidation immediately before execution
Approval doesn't make a proposal permanently valid. Immediately before calling any write API, execution must:
1. **Reload and verify the proposal from the server-side store:** check `content_hash` and `schema_version`.
2. **Check the approval:** `approvable_at(now)` is true, the approval matches `(proposal_id, content_hash)`, and the nonce hasn't been used yet.
3. **Confirm the account:** the authenticated account (`sts:GetCallerIdentity`) equals `account_id`.
4. **Check the policy:** the active policy still has the same `content_hash`. If it changed, re-propose. Never silently re-evaluate.
5. **Re-read current tags** for every `CHANGE_PROPOSED` resource, and compare them to `expected_state`. Any drift aborts that resource and reports it as stale, and the user must re-propose:
   - an ADD key that has appeared since;
   - an OVERWRITE key whose value differs from `expected_current_value`;
   - a resource that is gone;
   - for S3, any difference at all in the full tag set, because `PutBucketTagging` replaces the whole set.
6. **Write with tightly scoped credentials:** assume `CloudGuardTaggerRole` with a session policy limited to the approved ARNs, keys and values (see [phase4-iam-design.md](phase4-iam-design.md)).
7. **Verify after writing:** re-read the tags and confirm every approved change is present and no other tag changed. Then append the audit record, whether the write succeeded or failed.

## Not implemented in Phase 3
- Approval records, nonces and approver authentication.
- Execution, session-policy generation and post-write verification.
- Proposal persistence (proposals are returned, not stored).
