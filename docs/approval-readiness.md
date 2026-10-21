# Proposal → approval readiness (for Phase 4)

Phase 3 produced proposals. **Checkpoint 4.1** persists them with an append-only lifecycle. **Checkpoint 4.2** adds authenticated, server-side approve, reject and revoke decisions; see [authorization.md](authorization.md). **Execution is not implemented.** This document fixes the semantics that execution (Checkpoint 4.3) must build on.

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

## Implementation status
- **Implemented:**
  - proposal persistence and lifecycle (4.1);
  - authenticated decisions with nonces, idempotency, a versioned allow-list, separation of duties and approval expiry (4.2);
  - `DecisionService.verify_for_execution()`, the authorization re-check;
  - **fixture-only** revalidation and execution against a simulated account (4.3, see below).
- **Not implemented:** live AWS writers, session-policy generation and tagger-role deployment.

## Not implemented in Phase 3
The state of Phase 3, kept as history:
- Approval records, nonces and approver authentication.
- Execution, session-policy generation and post-write verification.
- Proposal persistence (proposals are returned, not stored).

## Fixture execution (Checkpoint 4.3)
**Fixture-only.** The executor changes tags in an in-memory `FixtureAccount`, never in AWS. Three things guarantee that:
- there is no live writer and no boto3 import in `executor.py` or `writers.py`;
- the executor refuses any non-FIXTURE identity provider, both at construction and on every call;
- the CLI's `proposal execute` accepts only `--mode fixture`, and there is no environment switch.

### Architecture
| Component | Module | Responsibility |
|---|---|---|
| Revalidator | `remediation/revalidation.py` | Pure per-resource decision: is the approved change still safe? |
| FixtureAccount | `remediation/writers.py` | Simulated Lambda, DynamoDB, S3 and EC2 state, with fault injection. It is also the discovery source in fixture mode. |
| FixtureWriter | `remediation/writers.py` | Applies `ApprovedWrite`s (add or overwrite only) to a `FixtureAccount`, and nothing else |
| Executor | `remediation/executor.py` | Authorization, lease, revalidation, write, verification, finalisation |
| Execution persistence | `remediation/repository.py` | `EXEC#<id>#META` (immutable), `#LEASE` (fencing) and `#EVT#<seq>` (append-only, hash-chained) |
| Audit | `remediation/audit.py` | `verify_execution()`: checks records, events, chain, lease tokens and the lifecycle outcome |

### What a request may contain
A request contains only: proposal ID, approval ID and an idempotency key. Identity comes from the IdentityProvider.
- **Never accepted:** resources, tag changes, expected tags, proposal bodies or authorization results from the caller.
- **The stored proposal is the only source of intent.**

### Lifecycle
1. **Authorize:** check the identity's mode (FIXTURE), then the `execute` permission on the proposal's account.
2. **Load:** the proposal is loaded and verified (hashes, history, decisions), with lazy expiry applied.
3. **Pre-flight checks:**
   - The simulated account must equal the proposal's account, else the whole execution is refused (`ACCOUNT_MISMATCH`).
   - The state must be APPROVED and the approval ID must match.
   - `verify_for_execution()` must pass: approval, expiry, mode, approver still authorized, overwrite acknowledgement.
   - The **current** tagging policy must still be the approved version; otherwise the proposal goes **STALE** and nothing is written.
4. **Start:** `APPROVED -> EXECUTING`, the execution record and the lease (token 1) are written in one transaction.
5. **Revalidate** every approved resource and record each result (`REVALIDATED`).
6. **Execute** each eligible resource:
   1. re-read it immediately before the write and revalidate again;
   2. write only the still-pending approved changes;
   3. read back and verify;
   4. record `RESOURCE_RESULT`.
7. **Finalize:** `EXECUTING -> SUCCEEDED | PARTIAL | FAILED` in one transaction with the `FINALIZED` event, and the lease is released.

An **ABORT** decision (Batch 2) can end an `EXECUTING` proposal at any point: `EXECUTING -> ABORTED`, see "Abort" below.

| Outcome | Meaning |
|---|---|
| SUCCEEDED | Every intended change verified, or already present |
| PARTIAL | At least one succeeded and at least one was skipped or failed. **No rollback**; remaining work needs a new proposal. |
| FAILED | No intended change succeeded (including "no writes because nothing was eligible") |
| ABORTED | Stopped by an ABORT decision. Writes already announced by a `WRITE_INTENT` are listed as `in_flight`; nothing else is written |
| STALE / EXPIRED / REVOKED | Reached before execution; nothing written |

Terminal proposals never execute again.

### Revalidation rules (per resource, independently)
| Status | When |
|---|---|
| `ELIGIBLE` | Safe to write; carries only the still-pending approved changes |
| `ALREADY_APPLIED` | Every approved key already has its approved final value; nothing is written |
| `RESOURCE_MISSING` | The resource no longer exists |
| `RESOURCE_RECREATED` | The immutable identity differs from the one captured at proposal time (DynamoDB `TableId`, EC2 instance ID, S3 creation date) |
| `TAG_DRIFT` | An ADD key now exists (or a case variant does), an OVERWRITE key changed or vanished, or (S3 only) any other bucket tag changed |
| `PROTECTED_TAG` | The resource is marked `cloudguard:protected`, or a key is reserved or invalid |
| `TAG_LIMIT_EXCEEDED` | It would exceed 50 user tags |
| `POLICY_CHANGED` | A value violates the approved policy version (defence in depth) |
| `ACCOUNT_MISMATCH` / `REGION_MISMATCH` | The resource's account or region differs from the approved record |
| `UNVERIFIABLE` | Reads kept failing transiently (throttling or server errors) after bounded retries |
| `INSPECTION_FAILED` | Tags couldn't be read (access denied) |

- **Lambda:** has no universal immutable identifier, so recreation under the same ARN **cannot be detected**. The result carries an `identity_note` saying so.
- **Unrelated tag changes** on Lambda, DynamoDB and EC2 don't block a write; they are never touched.

### Leases, idempotency and crash recovery
- **One lease per execution,** with an owner, a **fencing token** and an expiry (`LEASE_TTL`, 2 minutes, renewed on every recorded event).
  - Every audit append (an event, a write intent, the finalisation) is a conditional write on the lease, made in the same DynamoDB transaction or under the proposal lock locally. The conditions are:
    - same token;
    - not released;
    - **not expired** (`expires_ms` in DynamoDB).
  - An expired lease is never renewed, even if nobody has taken it over yet.
  - A takeover requires the old lease to have expired and bumps the token. Finalisation and abort release the lease.
- **Write intents (Batch 2 / M2):** before every write, the executor appends a `WRITE_INTENT` naming the resource, the exact changes and a `write_deadline`.
  - **The intent append is the ownership check.** It needs the lease to stay valid for at least `LEASE_SAFETY_MARGIN` (10 s), and it renews the lease to now + `LEASE_TTL`, so nobody can take over before then.
  - **Deadline:** the write must be issued before `write_deadline`, which is now + `WRITE_BUDGET` (30 s) and never later than the approval expiry. Retries stop at the deadline.
  - **Configuration check:** `WRITE_BUDGET + LEASE_SAFETY_MARGIN < LEASE_TTL` is enforced at construction.
  - **Ownership lost:** if an intent can't be recorded (takeover, finalisation, abort or expiry), the executor raises `LeaseLostError` and issues no further writes.
  - **Audit coverage:** every write reaching the account is preceded by a committed intent. A write whose outcome was never recorded is reported as `UNCONFIRMED_WRITE` when the execution is abandoned, or as `in_flight` when it's aborted.
- **Approval expiry is a hard write deadline (Batch 2 / M1, L2):**
  - The execution record stores `approval_expires_at`.
  - The start transaction is conditional on the head still holding this approval, unexpired at the start instant (`approval_expires_ms` in DynamoDB).
  - Resume, and every write announcement, re-check the expiry. After it, the remaining resources are recorded `NOT_ATTEMPTED`, and nothing is written.
  - An expired approval can't be revived by a resume or a retry.
- **Execution requests are idempotent.** The same key returns the same execution, including a finished or aborted one. A different requester or proposal with that key is a conflict.
- **Crash recovery:**
  - A later request (any authorized executor) resumes an `EXECUTING` proposal **after** the lease expires.
  - It takes over the lease with token+1, records `LEASE_RECOVERED`, and continues with resources that have no recorded outcome.
  - A write that landed before the crash shows up on the re-read as `ALREADY_APPLIED`. **It is never written twice**, and an unrecorded write is never assumed to have failed.
  - The remaining resources are recorded `NOT_ATTEMPTED`, with the execution finalized **without writes**, if any of these hold:
    - the approval has expired;
    - more than 30 minutes have passed since the start;
    - the approver is no longer authorized;
    - the tagging policy changed since approval.
- **Retries:** bounded (3 attempts) for throttling and transient errors only, and never past a write's deadline. There are **no retries** for access denied, validation, drift, account mismatch or protected tags.

### Abort (Batch 2 / M1)
`DecisionService.abort()`, or the CLI command `proposal abort ID --content-hash H --execution-id E --reason TEXT`.
- **Permission:** the `reject` permission, the same one revoke uses, in the execution's mode.
- **Binding:** bound to the running execution ID and the content hash, with a single-use nonce and an idempotency key.
- **One atomic write** does all of the following:
  - records the `ABORT` decision;
  - moves `EXECUTING -> ABORTED`;
  - **revokes the lease** (new token, released), so no executor can record or announce another write;
  - closes the execution's audit chain with an `ABORTED` event.
- **What abort can't stop:** a write already announced before the abort may still land, because no tagging API accepts a fencing token. It is listed in the summary's `in_flight`.
- `ABORTED` is terminal: it never resumes or restarts.

### Lease fencing: what is and isn't guaranteed (M2)
- **Guaranteed:**
  - an executor that has lost its lease (takeover, finalisation, abort or expiry) cannot announce or record another write;
  - every write is preceded by an audit record;
  - no write is issued after its deadline or after the approval expiry, measured by the executor's clock.
- **Not guaranteed (mitigated, bounded, not eliminated):**
  - AWS tagging APIs accept no fencing token. A write that passed its last deadline check can still reach AWS late if the process pauses, the network is slow, or clocks disagree by more than the safety margin.
  - DynamoDB conditions compare against the writer's clock, because conditions have no server time.
  - A serialized writer or a token-aware resource API would be needed to close the race completely; neither exists for AWS resource tags.

### Time-of-check to time-of-use (TOCTOU)
AWS tagging APIs (`TagResource`, `CreateTags`, `PutBucketTagging`) have **no conditional writes** tied to the tag state that was read. CloudGuard re-reads immediately before each write and verifies afterwards, which **narrows but does not close** the race. The tests simulate concurrent changes:
- **Detected:** a change before the pre-write re-read (the resource is skipped); a change to unrelated tags during the write (recorded as a warning); a resource replaced during the write (verification fails).
- **Not detected:** another actor setting the *same* key in the instant between the re-read and the write. Our approved value silently replaces it, and this is indistinguishable afterwards.

The fixture executor **does not prove production AWS write safety**.

### Why this does not authorize live remediation
Live writers are deliberately absent. Before they can be considered:
1. **Deploy and validate the IAM design:** validate the tagger role and per-approval session policies with IAM Access Analyzer and a sandbox account, and close the 9 open assumptions in [phase4-iam-design.md](phase4-iam-design.md).
2. **Session policies:** generate them from `approval_binding()`, with chunking and size tests.
3. **Live readers:** produce `ResourceSnapshot`s with immutable identities: `DescribeTable.TableId`, instance ID, bucket creation date, and `ExpectedBucketOwner` for S3.
4. **Live writers:**
   - Lambda `TagResource`, DynamoDB `TagResource`, EC2 `CreateTags`, and S3 read-merge-`PutBucketTagging` that never drops a key.
   - Each must be stub-tested, kept separate from fixture code, and require an explicit, separately approved configuration.
5. **Eventual consistency:** handle the real tag-read consistency behaviour in verification, and rate-limit DynamoDB tagging.
6. **Server-side identity** (Phase 7) instead of local CLI trust.
