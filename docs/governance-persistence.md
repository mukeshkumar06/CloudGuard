# Governance persistence (single DynamoDB table)

Table `cloudguard-governance` uses on-demand capacity, string keys `PK` and `SK`, and point-in-time recovery. It's defined in `infra/cloudguard-core.yaml` and **not yet deployed**.

## New-table deployment, no data migration
An earlier draft of the template defined `cloudguard-scans`, with lowercase `pk`/`sk` keys. Phase 2 replaced it with `cloudguard-governance` (`PK`/`SK`).
- **Never deployed.** Checked on 2026-10-14: `DescribeTable` returned "not found" for both names in ap-south-1, and there were no CloudGuard stacks.
- **This is a new-table deployment.** There is **no data migration**, because there was never any data.
- **A rename of a live table would replace it.** If an older copy of the template *had* been deployed, CloudFormation would treat a change of `TableName` or key schema as a replacement: it creates a new, empty table and **deletes the old one along with its data**. CloudGuard has no migration tooling. Export the data first (for example with DynamoDB export to S3) before any such change.

| Entity | PK | SK | Status |
|---|---|---|---|
| Policy version (immutable) | `POLICY#<policy_id>` | `VERSION#<10-digit version>` | **Implemented** |
| Active policy pointer | `POLICY#<policy_id>` | `ACTIVE` | **Implemented** |
| Scan summary | `ACCOUNT#<account_id>` | `SCAN#<started_at ISO>#<scan_id>` | Planned (Phase 5) |
| Finding | `SCAN#<scan_id>` | `FINDING#<finding_id>` | Planned (Phase 5) |
| Proposal | `PROPOSAL#<proposal_id>` | `META` | Planned (Phase 4/5) |
| Audit record (append-only) | `ACCOUNT#<account_id>` | `AUDIT#<timestamp ISO>#<operation_id>` | Planned (Phase 4/5) |

The policy access patterns are implemented in `cloudguard/governance/policy_store.py`. The other rows are a design proposal and may change when Phases 4 and 5 are built.

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
