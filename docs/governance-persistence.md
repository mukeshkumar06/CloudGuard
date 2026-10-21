# Governance persistence (single DynamoDB table)

Table `cloudguard-governance` uses on-demand capacity, string keys `PK` and `SK`, point-in-time recovery, and **no TTL** (disabled on purpose; see "TTL" below). It's defined in `infra/cloudguard-core.yaml` and **not yet deployed**.

## New-table deployment, no data migration
An earlier draft of the template defined `cloudguard-scans`, with lowercase `pk`/`sk` keys. Phase 2 replaced it with `cloudguard-governance` (`PK`/`SK`).
- **Never deployed.** Checked on 2026-10-14: `DescribeTable` returned "not found" for both names in ap-south-1, and there were no CloudGuard stacks.
- **This is a new-table deployment.** There is **no data migration**, because there was never any data.
- **A rename of a live table would replace it.** If an older copy of the template *had* been deployed, CloudFormation would treat a change of `TableName` or key schema as a replacement: it creates a new, empty table and **deletes the old one along with its data**. CloudGuard has no migration tooling. Export the data first (for example with DynamoDB export to S3) before any such change.

| Entity | PK | SK | `ttl_epoch` (retention hint only, never deleted) | Status |
|---|---|---|---|---|
| Policy version (immutable) | `POLICY#<policy_id>` | `VERSION#<10-digit version>` | never | **Implemented** (Phase 2) |
| Active policy pointer | `POLICY#<policy_id>` | `ACTIVE` | never | **Implemented** (Phase 2) |
| Proposal body (immutable) | `PROPOSAL#<id>` | `META` | never | **Implemented** (Checkpoint 4.1) |
| Proposal state head (materialised view; numeric `approval_expires_ms` for the start condition, Batch 2) | `PROPOSAL#<id>` | `HEAD` | never | **Implemented** (Checkpoint 4.1) |
| Lifecycle event (append-only) | `PROPOSAL#<id>` | `EVENT#<10-digit seq>` | never | **Implemented** (Checkpoint 4.1) |
| Idempotency record (temporary) | `IDEMP#<scope>#<key>` | `META` | `ttl_epoch` = created + 7 days | **Implemented** (Checkpoint 4.1) |
| Decision record (approve/reject/revoke, append-only) | `PROPOSAL#<id>` | `DECISION#<decision-id>` | never | **Implemented** (Checkpoint 4.2) |
| Decision nonce (single-use) | `NONCE#<nonce>` | `USED` | `ttl_epoch` = approval expiry + 7 days | **Implemented** (Checkpoint 4.2) |
| Authorization allow-list version / pointer | `AUTHZ#<config-id>` | `VERSION#<n>` / `ACTIVE` | never | **Implemented** (Checkpoint 4.2) |
| Execution record (immutable) | `PROPOSAL#<id>` | `EXEC#<execution-id>#META` | never | **Implemented** (Checkpoint 4.3, fixture executions only) |
| Execution lease (fencing token, owner, expiry, released; numeric `expires_ms` checked by every fenced write, Batch 2) | `PROPOSAL#<id>` | `EXEC#<execution-id>#LEASE` | never | **Implemented** (Checkpoint 4.3) |
| Execution event (append-only, hash-chained; incl. `WRITE_INTENT` and `ABORTED`, Batch 2) | `PROPOSAL#<id>` | `EXEC#<execution-id>#EVT#<10-digit seq>` | never | **Implemented** (Checkpoint 4.3) |
| Execution idempotency record | `IDEMP#proposal-execute#<key>` | `META` | `ttl_epoch` = created + 7 days | **Implemented** (Checkpoint 4.3) |

All Checkpoint 4.3 additions are additive. `StoredProposal` gained `resource_identities`, which is folded into the binding digest; `ProposalHead` gained `execution_id`; allow-lists gained an optional `execute` list.

### Execution writes (Checkpoint 4.3)
- **Start:** one transaction:
  1. `Update HEAD` (`APPROVED -> EXECUTING`, sequence-conditioned).
  2. `Put EVENT#<seq>`.
  3. `Put EXEC#<id>#META`.
  4. `Put EXEC#<id>#LEASE` (token 1).
  5. `Put IDEMP#proposal-execute#<key>`.
- **Each execution event:** one transaction:
  1. `Update LEASE`, conditioned on `token = :current AND released = false` (fencing), which also renews the expiry.
  2. `Put EXEC#<id>#EVT#<seq>`, conditioned on `attribute_not_exists`.

  Fencing uses a conditional **Update** of the lease item rather than a `ConditionCheck`, so no `dynamodb:ConditionCheckItem` permission is needed.
- **Lease takeover:** `Update LEASE`, conditioned on the previous token and the previous lease content.
- **Finish:** one transaction:
  1. `Update HEAD` (`EXECUTING -> SUCCEEDED | PARTIAL | FAILED`).
  2. `Put EVENT`.
  3. Fenced `Update LEASE` with `released = true`.
  4. `Put EXEC#<id>#EVT#<seq>` (`FINALIZED`).
- **Hash chain:** each execution event carries `prev_hash` and `event_hash`; the first event chains from the execution record's hash. `verify_execution()` recomputes the chain on every read. It detects modified, dropped or reordered events. **It is not a signature:** an actor able to rewrite the whole partition could recompute it.
| Scan summaries and findings | `ACCOUNT#<id>` / `SCAN#<id>` | `SCAN#...` / `FINDING#...` | never | Planned (Phase 5) |

## TTL: disabled on the governance table (H2 follow-up, Option D)
**The governance table has no DynamoDB TTL, and it must stay that way** while participants and administrators can write arbitrary attributes on the items they may write.

**Why:**
- DynamoDB TTL is table-wide and keys only on an attribute name.
- With TTL on `ttl_epoch`, any participant could set `ttl_epoch` in the past on any `PROPOSAL#`, `IDEMP#` or `NONCE#` item, and any administrator on `AUTHZ#` or `POLICY#` items. DynamoDB would then delete them, even though no one is granted `DeleteItem`.
- Deleting every `AUTHZ#` item would make `active_version()` return `None` and reopen the allow-list bootstrap.
- **Re-enabling TTL reopens this deletion vulnerability.** `backend/tests/test_iam_boundaries.py` asserts the table has no `TimeToLiveSpecification`, and that no policy grants `dynamodb:UpdateTimeToLive`.

**Effect on the application:**
- No code reads `ttl_epoch` or relies on records disappearing.
- Idempotency and nonce records are now kept indefinitely, as they already were in the local stores:
  - a client-supplied idempotency key stays bound to its first request;
  - nonces are permanently single-use.
- Storage growth is a few hundred bytes per create, decision or execution.
- If purging is ever needed, the clean option is a **separate ephemeral table** for `IDEMP#`/`NONCE#` records, with its own TTL (Option C, deferred).

**What this does not fix:** overwrite and forgery through direct table access remain possible. Participants and administrators can still replace items with unconditional `Put`s, and IAM can't require write conditions. See `aws-permissions.md` § "Known limitations".

### History (Checkpoint 4.1)
DynamoDB TTL **deletes** items. Earlier, `cloudguard-core.yaml` named the TTL attribute `expires_at`, which is also the name of a business concept (proposal expiry). A proposal or audit item written with an `expires_at` number would have been silently deleted.

These are now separate attributes:
- **`proposal_expires_at`:** application-level expiry, an ISO-8601 string stored on proposal META and HEAD. Never used for TTL.
- **`ttl_epoch`:** written **only** on idempotency and nonce records (epoch seconds). Originally the TTL attribute; since Option D it is an **inert retention hint**, because the table has no TTL.
  - Retention must exceed the validity of anything that references the record.
  - Idempotency records live 7 days; proposals live at most 1 hour.

The application never writes `ttl_epoch` on: proposal META and HEAD, lifecycle events, approval records, execution records. Since Option D, that attribute has no effect on any item anyway.

The correction is a template change only. **Nothing has been deployed**, and no table exists in AWS.

## Proposal writes (Checkpoint 4.1)
Each operation is a single `TransactWriteItems` call.

- **Create:** four conditional puts.
  - `META`, `HEAD`, `EVENT#0000000001` and `IDEMP#...`, each conditioned on `attribute_not_exists(PK)`.
  - If the transaction is cancelled, the repository reads the idempotency record. The **same** request fingerprint returns the original proposal (`created: false`). A different fingerprint is an `IdempotencyConflictError`.
- **Lifecycle transition** (only lazy expiry is wired in 4.1):
  1. `Update HEAD`, conditioned on `seq = :expected AND state = :expected_state AND content_hash = :hash`.
  2. `Put EVENT#<seq+1>`, conditioned on `attribute_not_exists(PK)`.

  A cancelled transaction becomes `TransitionConflictError`. Expiry re-reads after a conflict, so concurrent expiry appends exactly one event.
- **Reads:** all reads use `ConsistentRead`. Every read of a proposal:
  - re-validates the stored `TaggingProposal` (content hash, schema version, approval-binding digest, request fingerprint and policy reference against the stored metadata);
  - replays the event stream to verify the head;
  - repairs a head that fell *behind* its events (a crash between the event write and the head write);
  - rejects a head that is ahead of, or contradicts, the events.
- **Append-only:** META and EVENT items are only ever written with conditional `PutItem`. The application never issues `UpdateItem` or `DeleteItem` against them.
- **Decision (4.2):** one transaction of five entries:
  1. `Update HEAD`, with the same conditions as a transition.
  2. `Put EVENT#<seq+1>`.
  3. `Put DECISION#<id>`.
  4. `Put NONCE#<n>`.
  5. `Put IDEMP#proposal-decision#<key>`.

  Entries 2–5 are each conditioned on `attribute_not_exists(PK)`. If the transaction is cancelled, the repository checks in order: a used nonce raises `NonceReplayError`; an existing idempotency record is a replay or a conflict; anything else is `TransitionConflictError`. Reads verify that every decision event references exactly one matching decision record, so tampered, orphaned or forged decisions are detected.
- **IAM:** `UpdateItem` was added to the template's table permissions, because the transactional HEAD update is authorized as `dynamodb:UpdateItem`. No `DeleteItem` is granted. Not deployed.

## Local proposal store: SQLite (M4)
`LocalProposalRepository(directory)` keeps every record in **one SQLite database**, `<directory>/proposals.sqlite3`. The in-memory store is used only for tests and ephemeral runs.

**Layout and formats**
- Records keep their **logical names and JSON bodies** (`proposals/<id>/META.json`, `EVENT-<seq>.json`, `idempotency/…`, `nonces/…`) in a table `records(name PRIMARY KEY, body)`. The record formats are unchanged.

**Atomic writes**
- Every multi-record operation runs in **one write transaction** (`BEGIN IMMEDIATE … COMMIT`): create, decision, state transition and lazy expiry, execution start, execution event, finalisation, abort, lease takeover, head repair.
- Each operation commits fully or leaves the previous state unchanged. That holds when an exception escapes, including a simulated crash, and when the process is killed: SQLite discards uncommitted WAL frames.
- So an orphan decision, a partial finalisation or a missing idempotency record can no longer happen.
- The in-memory store gives the same all-or-nothing behaviour by restoring a snapshot.

**Consistent reads**
- Multi-record integrity reads (`ProposalManager.get`/`history` and execution views) run in **one read transaction**, a WAL snapshot, so a concurrent commit can't make a reader see half of a write.

**Locking**
- SQLite holds the locks, and the OS releases them when a process dies. There are **no lock files**, so a crashed process can't leave a stale lock behind.
- Writers wait up to 30 s for the write lock, then fail with a retryable error.

**Durability: `journal_mode=WAL`, `synchronous=FULL`**
- Every commit is on disk before it returns, which matters for the audit trail.
- `synchronous=NORMAL` would be faster, but could lose the most recent commits on power loss or an OS crash; it would still be consistent.

**Local disks only**
- Network shares and synced folders (OneDrive, Dropbox, SMB) are **unsupported**: WAL needs working shared memory and byte-range locks.
- If WAL can't be enabled, the store refuses to open (`StoreUnavailableError`).

**Unchanged**
- Path safety: the same lexical name check.
- Validation: corrupt or invalid records raise `ProposalIntegrityError`.
- **Migration:** the old one-file-per-record layout isn't read. It was unreleased and git-ignored; delete any old `.cloudguard/proposals/` content.
- The allow-list store (`LocalAuthzStore`) and the local policy store still use files. They weren't part of M4.

## DynamoDB: read consistency, cancellations and retries (M4)
**Reads**
- DynamoDB has no multi-item snapshot. Readers read the HEAD, then the records, then the HEAD again.
- Every commit that adds an event or a decision also advances the HEAD's `seq`, so an unchanged HEAD means the records belong to it. For executions, the HEAD and the lease are compared.
- A moved HEAD is retried up to 5 times, then `ConsistentReadError` is raised. That error is **retryable**, never a tampering finding. Integrity and hash checks run only on a consistent set and are unchanged.

**Cancellation mapping**
- `TransactionCanceledException.CancellationReasons` is read from botocore's parsed error (`exc.response["CancellationReasons"]`, one entry per `TransactItems` entry in request order). It is mapped per role:
  - an idempotency-record conflict means a replay or conflict (`IdempotencyKeyTakenError`);
  - a nonce conflict means `NonceReplayError`;
  - a head, event or decision conflict means `TransitionConflictError`;
  - a lease conflict means `LeaseConflictError`;
  - `TransactionConflict` or throttling means `TransientStoreError` (nothing written, retry);
  - any other code re-raises the original error.
- When reasons are missing, or don't line up with the transaction's items, the earlier follow-up-read inference is used.
- Tests use a mock client and moto, **not** AWS.

**`ClientRequestToken`**
- botocore fills it automatically, once per API call: `idempotencyToken` in the service model, `before-parameter-build`. Its own retries resend the same request with the same token.
- **A stable token derived from the application idempotency key is deliberately not used.** An application-level retry rebuilds the request with a new decision ID, time and nonce. Reusing a token with different parameters fails with `IdempotentParameterMismatchException` within DynamoDB's token window.
- Application retries are handled by the idempotency records, which are written in the same transaction.
- **Not yet verified against AWS:** the token window, the deduplication behaviour, and the exact cancellation reasons DynamoDB returns.

## Policy writes
- Saving a policy is **one `TransactWriteItems` call** with two puts:
  1. Write the new version item, conditioned on `attribute_not_exists(PK)` so a version can never be overwritten.
  2. Move the `ACTIVE` pointer, conditioned on `version = :expected`, or on `attribute_not_exists(PK)` when the policy is new.
- A cancelled transaction surfaces as `PolicyConflictError`: someone else saved first, or the caller's view was stale.
- Reads use `ConsistentRead`. History is a single `Query` on `PK = POLICY#<id>` with `begins_with(SK, "VERSION#")`.
- Policy documents are stored as validated JSON in a `document` string attribute, so DynamoDB type mapping can't alter them.

## Planned design notes (not implemented)
- **Scans:** the newest-first list per account is a reverse `Query` on `ACCOUNT#<id>` with `begins_with(SK, "SCAN#")`. Findings for a scan are one `Query` on `SCAN#<id>`.
- **`whats_changed_since`:** compares two scans' findings by `finding_id`, which is stable per resource ARN.
- **Audit records:**
  - Writes are only puts conditioned on `attribute_not_exists(SK)`.
  - The application never issues `UpdateItem` or `DeleteItem` for `AUDIT#` items.
  - The table's IAM policy can deny those actions for audit keys via `dynamodb:LeadingKeys` once Phase 5 lands.
- **Findings:** store `existing_tags`, missing/invalid tags, notices and evidence, but not a full copy of the AWS resource description.
