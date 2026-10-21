# Identity and authorization (Checkpoint 4.2)

## Identity: who is calling
Identity always comes from an **IdentityProvider**. No method takes a principal, a display name or a free-text decision as an argument, and nothing a user or AI model writes can become authorization.

| Mode | Provider | Identity | Valid for |
|---|---|---|---|
| `LIVE` | `StsCallerIdentityProvider` | The caller's own AWS credentials, verified by `sts:GetCallerIdentity` | Real AWS accounts |
| `FIXTURE` | `DevelopmentIdentityProvider` | `dev:<name>`, **never authenticated** | Synthetic data and tests only; refused by every LIVE operation |

**Canonical identities** are what the allow-list matches. Separation of duties compares **people**; see "People and separation of duties (M3)" below.

`GetCallerIdentity` returns only `Account`, `Arn` and `UserId`. CloudGuard captures `UserId` too: `AIDA…` for an IAM user, `<role unique id>:<session name>` for an assumed role, and the account id for root. It checks that the `UserId` is consistent with the ARN. It **never** sees a source identity or session tags, because `GetCallerIdentity` doesn't return them.

| Caller ARN (from STS) | Canonical id |
|---|---|
| `arn:aws:iam::<acct>:user/<path>/<name>` | `arn:aws:iam::<acct>:user/<name>` (path dropped) |
| `arn:aws:sts::<acct>:assumed-role/<role>/<session>` | `arn:aws:iam::<acct>:role/<role>` (**session dropped**) |
| `arn:aws:iam::<acct>:root` | unchanged |
| federated users, other types | **rejected** |

The session name is dropped from the canonical id because the caller usually chooses it. It identifies a person **only** through a `session_attributed_role` binding, in which an administrator asserts that the role's trust policy enforces it.

**Trust boundary:** the CLI runs locally with the caller's credentials, so the local process is trusted. A server deployment (Phase 7) must establish identity server-side, for example with a presigned `GetCallerIdentity` or OIDC.

## Allow-list configuration
```json
{
  "schema_version": "cloudguard.authz/v1",
  "accounts": {
    "<12-digit account id>": {
      "admin":   ["arn:aws:iam::<acct>:role/GovernanceAdmin"],
      "create":  ["arn:aws:iam::<acct>:role/GovernanceRequester"],
      "approve": ["arn:aws:iam::<acct>:role/GovernanceApprover"],
      "reject":  ["arn:aws:iam::<acct>:role/GovernanceApprover", "arn:aws:iam::<acct>:role/GovernanceRequester"]
    }
  }
}
```
- **Permissions:**
  - `admin` (Batch 1 / H1) changes that account's entries in the allow-list itself. See "Administration" below.
  - `create` stores a proposal.
  - `approve` approves.
  - `reject` rejects a PENDING proposal **and** revokes an APPROVED one. Revocation is a distinct `REVOKED` state, never `REJECTED`.
  - `execute` (Checkpoint 4.3) requests execution of an approved proposal. It's optional and defaults to nobody, so configurations without it stay valid and simply allow no execution.
- **Entries:** exact IAM user or role ARNs **of that same account** (IAM paths are ignored), the account root, or `dev:<name>`.
  - **No wildcards.**
  - `dev:` entries are honoured only in FIXTURE mode, and ARN entries only in LIVE mode.
- **Nothing is hardcoded:** with no saved configuration, every request is denied. The synthetic demo configuration is `backend/fixtures/authz-demo.json` (development identities, example account `123456789012`).
- **Validation before saving:** schema version, 12-digit accounts, entry format, account consistency, duplicates (after canonicalisation), and unknown fields.

## Versioning
- Every save creates an immutable version: `(config_id, version, content_hash, record_hash)`.
  - `content_hash` covers the configuration only; it's what decisions reference.
  - `record_hash` covers the whole version, including its `AuthzChange` audit record, `created_by`, the note, `initialized_modes` and `previous_record_hash`, the hash of the version it replaced. Versions are verified on every load, and `authz history` checks the chain.
  - This is integrity, not a signature: anyone who can rewrite the store can recompute it. See "Overwrite and forgery remain possible" under [Known limitations (participant scope)](aws-permissions.md#known-limitations-participant-scope) in `aws-permissions.md` (H2).
- Saves use optimistic concurrency (`--expected-version`). The caller's authority is checked against exactly that version, and the commit is conditional on it still being active. An unchanged config is rejected.
- **Storage:**
  - Local: `.cloudguard/authz/<id>/VERSION-<n>.json` plus `ACTIVE.json`, the commit point. Commits take an on-disk lock and re-read the pointer, so separate processes serialise.
  - DynamoDB: `AUTHZ#<id>` / `VERSION#<n>` (immutable) and `ACTIVE` (pointer), written in one conditional transaction, with no TTL.
- **New decisions** are evaluated against the **active** version. Each decision records the exact version, its hash and the matched entry (`AuthorizationBasis`).
- **Re-checked later:** a change to the allow-list affects later decisions **and** the execution re-check. An approver removed from the list can no longer authorize execution of something they approved earlier.

```bash
python -m cloudguard.governance authz validate my-authz.json
python -m cloudguard.governance authz save my-authz.json --expected-version 0 --mode fixture --dev-identity admin
python -m cloudguard.governance authz save my-authz.json --expected-version 1 --mode live --profile <PROFILE>
python -m cloudguard.governance authz history
```

## Administration (Batch 1 / H1)
Saving a new allow-list version is itself an authorized action.

**Identity**
- Every save is made by a principal from an **IdentityProvider**: STS caller identity in LIVE mode, `dev:<name>` in FIXTURE mode.
- `created_by` and the `AuthzChange` record are copied from that verified principal: actor, exact STS caller ARN, verified account, authentication method and time. They are never taken from a flag.
- The old `--author` label was removed. `--note` is free text and never identity.

**LIVE and FIXTURE are isolated**
- `dev:` entries are FIXTURE entries; ARN and root entries are LIVE entries.
- A save in one mode may not change the other mode's entries, so an unauthenticated development identity can never grant a LIVE permission.
- A LIVE save with a development identity is refused.

**Bootstrap (one time per mode)**
- The first save of a mode must be made by the administrator configured **explicitly** in the environment variable `CLOUDGUARD_AUTHZ_BOOTSTRAP_ADMIN`.
  - The value is an exact principal: `arn:aws:iam::<ACCOUNT_ID>:role/<AdminRole>` (LIVE) or `dev:<name>` (FIXTURE).
  - It may hold one LIVE and one FIXTURE principal, comma-separated.
  - The CLI reads it only for `authz save`. Library callers pass `AuthzManager(..., bootstrap_admin=...)`; the default is none, so no bootstrap is possible.
- The bootstrap save must list that administrator under `admin` for every account it defines.
- A LIVE bootstrap may define **only the caller's own STS-verified account**.
- Concurrent bootstraps race on the same conditional commit, so exactly one succeeds.

**After bootstrap**
- The mode is recorded in `initialized_modes`, which every later version inherits. From then on, the bootstrap setting is **never consulted** for that mode, whatever its value.
- Removing every administrator of a mode is refused, so bootstrap can't be reopened.
- Unset the variable once initialization is done; it is harmless but unnecessary.

**Changes after bootstrap**
- **Any change:** for every account whose entries change, the caller must hold `admin` on that account in the active version.
- **LIVE:**
  - a caller may only change its **own** account;
  - LIVE accounts can't be added or removed after bootstrap (multi-account is out of scope for v1).
- **FIXTURE:** an existing administrator may add a new account.
- **Lockout guard:** every account must keep at least one administrator of the mode being saved.

**Failures write nothing**
- Every check runs before the single conditional commit. Refused, stale or unchanged saves leave the active version untouched.
- **Exit codes:** 3 for denied, 1 for conflict or error.

**Limits**
- The bootstrap administrator is chosen by whoever controls the configuration: the operator for the local CLI, server-side configuration for a future server deployment.
- Store-level protection still depends on IAM. This is the H2 finding: the runtime role can currently write the table directly.

## Decisions
`DecisionService.approve | reject | revoke`. Every decision is bound to:
- the exact `proposal_id` **and** `content_hash`, which must match the stored proposal;
- the authenticated principal (`AWS_PRINCIPAL`) or the development principal (`DEVELOPMENT`, FIXTURE only);
- the authorization basis;
- a **single-use nonce** (22–128 URL-safe characters) and an **idempotency key**. Repeating the same request returns the same decision. Reusing a key for a different request, proposal or principal is a conflict.

It's written **atomically** in one transaction (or under one lock locally): the decision record, the lifecycle event, the head update, the nonce record and the idempotency record.

**Rules:**
- **Approve requirements:** the proposal must be PENDING and `approvable_at(now)`.
- **Overwrites:** proposals that overwrite existing values need `--acknowledge-overwrites`, and the acknowledgement is recorded.
- **Approval expiry:** `min(decision time + 15 minutes, proposal expiry)`. After it, the proposal lazily becomes `EXPIRED`, with reason "approval expiry reached".
- **LIVE mode:**
  - the caller's STS account must equal the proposal's account;
  - the proposal must have been created by an authenticated principal;
  - **requester ≠ approver**, compared by **person** (M3).
- **FIXTURE mode:** the same development identity may create and approve, for testing, unless `separation.fixture_approver_must_differ_from_requester` is set.
- **Every decision** (approve, reject, revoke, abort) and every proposal creation resolves the caller to a person, and fails closed if it can't. It records `IdentityEvidence`: the person ID, the binding kind, the principal ARN, the STS `UserId`, and the allow-list version and hash.

## People and separation of duties (M3)
Every account in the allow-list carries **person bindings**. Each one maps an IAM principal to exactly one person ID (1–63 characters of `[a-z0-9._-]`).

| Binding `kind` | Principal | Person comes from | Trust basis |
|---|---|---|---|
| `iam_user` | exact IAM user ARN | `person` | STS authenticates the user |
| `single_person_role` | exact role ARN | `person` | **Administrator assertion** that the role's trust policy admits only that person |
| `session_attributed_role` | exact role ARN | `sessions[<session name>]` | **Administrator assertion** (`session_source`): `"iam-username"` means the trust policy enforces `sts:RoleSessionName = ${aws:username}`; `"identity-center"` means the sessions are named by IAM Identity Center |

```json
"bindings": [
  {"kind": "iam_user", "principal": "arn:aws:iam::<acct>:user/alice", "person": "alice", "unique_id": "AIDA..."},
  {"kind": "single_person_role", "principal": "arn:aws:iam::<acct>:role/BobApprover", "person": "bob"},
  {"kind": "session_attributed_role", "principal": "arn:aws:iam::<acct>:role/Governance",
   "session_source": "iam-username", "sessions": {"carol": "carol", "dave": "dave"}}
]
```

**Rules**
- **Every LIVE entry must be bound.** A config with an unbound ARN entry is rejected. Root can't be bound, so it can't appear in LIVE entries. At run time, an unbound principal, an unattributed session, a binding of the wrong kind or a mismatched pin fails closed with `PersonResolutionError` (CLI exit code 3).
- **Optional `unique_id` pin** (`AIDA…` for users, `AROA…` for roles). It must equal the STS unique id, so a deleted and recreated user or role with the same name doesn't inherit the binding.
- **A person can't hold both `admin` and `approve` on one account.** This is checked on every save, through the bindings, in both modes.
- **Requester ≠ approver** (people): always in LIVE. In FIXTURE only if `fixture_approver_must_differ_from_requester`, which is off by default.
- **Executor ≠ approver** (people): always, in every mode, checked before every start and every resume.
- **Executor ≠ requester** (people): configurable per mode with `separation.live_executor_must_differ_from_requester` / `fixture_executor_must_differ_from_requester`. Both default to off.
- **FIXTURE identities map implicitly:** `dev:<name>` → person `<name>`.
- **Mode isolation:** bindings and the LIVE separation setting are LIVE data. A FIXTURE save can't change them, and the FIXTURE settings belong to FIXTURE saves (Batch 1 mode isolation).
- **Allow-list changes record the person** who made them (`AuthzChange.person_id`). A LIVE bootstrap config must bind its bootstrap administrator.
- **Evidence and tampering:**
  - decision records carry `identity`, cross-checked against the lifecycle event (`person_id`, `binding_kind`);
  - the proposal stores `requester_identity`, cross-checked against the creation event;
  - execution records carry `executor_identity` and `approver_identity`, cross-checked against the start and approval events and covered by the execution hash chain.

**What this does not do**
- **Administrator assertions about trust policies are not verified by CloudGuard.** It makes no IAM calls and doesn't inspect trust policies. A misconfigured `single_person_role` or `session_attributed_role` lets someone act as the bound person.
- **Administrators are trusted.** An administrator can bind a second principal to a new, fictitious person. A two-administrator rule for allow-list changes would be a separate feature.
- **Shared credentials, and a human holding several principals bound to different people,** can't be detected without a single trusted identity provider.
- **Source identity can't be read by the application.** It can only be enforced by IAM (`sts:SourceIdentity`) and recorded in CloudTrail.
- **Direct DynamoDB access bypasses all of this** (H2 limitation). Real enforcement needs Phase 7 server-mediated writes or signed records.
- **Not yet verified against AWS:** the `UserId` formats across role chaining; `sts:RoleSessionName` and `sts:SourceIdentity` trust-policy behaviour; IAM Identity Center session naming.
- **Policy changes:** if the tagging policy changed since the proposal was created, approval is refused and the proposal becomes `STALE`.
- **Terminal states:** `REJECTED`, `REVOKED`, `EXPIRED` and `STALE` accept no further decisions.

## Execution re-check (no execution yet)
`DecisionService.verify_for_execution(proposal_id)` is what Checkpoint 4.3 must call immediately before executing. It re-establishes the caller's identity and returns `allowed` only if all of these hold:
- **State:** the proposal is APPROVED and the approval hasn't expired.
- **The approval record matches:** it exists, is an APPROVE decision, and has the proposal's content hash.
- **Mode:** the approval's mode equals the current mode. FIXTURE approvals **never** authorize LIVE operations.
- **Account:** in LIVE mode, the caller's account equals the proposal's account.
- **Still authorized:** the approver is still on the **current** allow-list.
- **Overwrites:** any required overwrite acknowledgement is present.

## Execution authorization (Checkpoint 4.3, fixture only)
`FixtureRemediationExecutor.execute()` authorizes, in order:
1. **Identity:** from the IdentityProvider, in **FIXTURE mode only**. Live providers are refused at construction and on every call.
2. **Requester:** must hold the `execute` permission on the proposal's account.
3. **Approval:** `verify_for_execution()` must pass. That covers the approval record, content hash, expiry, mode, overwrite acknowledgement, and the **original approver still being on the current allow-list**.
4. **Policy:** the active tagging policy must still be the approved version; otherwise the proposal becomes STALE.

**Resuming a crashed execution** re-checks:
- the requester's `execute` permission;
- the approver's current authorization;
- **the approval expiry** (Batch 2 / M1: an expired approval never resumes);
- the current tagging policy.

Resumption is also bounded to 30 minutes after the start. If any check fails, the remaining work is closed without writes. The same expiry, policy and approver checks run again before every write.

**Abort (Batch 2 / M1):** `DecisionService.abort()` stops an `EXECUTING` proposal.
- It requires `reject`, the same permission revoke uses, and is bound to the running execution ID.
- It is recorded as an `ABORT` decision, `EXECUTING -> ABORTED`, revokes the execution lease, and closes the execution audit.
- See `approval-readiness.md` § "Abort".

## Limitations
- **Nonces are client-supplied** (the CLI generates one if omitted). A server-issued challenge is the stronger option once there's a server (Phase 7).
- **Local trust:** STS identity in the CLI proves only what the local credentials are. A compromised local machine is out of scope.
- **Same-role users:** two people sharing one IAM role are one person unless the role is bound as a `session_attributed_role`. Otherwise they can't approve each other's proposals, which fails closed.
