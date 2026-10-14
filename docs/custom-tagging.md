# Custom tagging requests (Phase 3, read-only)

Custom tagging turns a natural-language request into a **hashed, expiring, read-only proposal**. Nothing is written to AWS in this phase. Approval and execution are Phase 4, which will bind approval to the proposal's `content_hash`.

Custom tagging is separate from policy compliance. A custom tag (for example `ServiceWRK=Lambda`) is labelled `CUSTOM` and never becomes a mandatory policy tag.

## Workflow
```
text ──► parser ──► server-side validation ──► fresh discovery ──► target resolution ──► policy check ──► proposal
         (rules; Bedrock     (AWS tag rules,        (read-only;          (exact match or       (per resource    (exact resources,
          fallback later)     empty values)          current tags)        clarification)        type)            current tags, changes)
```
| Module | Role |
|---|---|
| `custom_tagging/parser.py` | `RuleBasedParser` (deterministic) and `CompositeParser`, the hook for a later Bedrock fallback |
| `custom_tagging/validation.py` | AWS tag constraints, and POLICY vs CUSTOM classification |
| `custom_tagging/resolver.py` | Specific or bulk resolution over inspected discovery results |
| `custom_tagging/engine.py` | `CustomTaggingEngine.prepare(text)` and `prepare_request(request)` |
| `custom_tagging/models.py` | Request, proposal and outcome models, with integrity validators |

## Supported requests
| Operation | Example |
|---|---|
| `ADD_TAG` | `Tag the Lambda function ServiceWRK with ServiceWRK=Lambda` · `Add Owner=core to dynamodb table orders` |
| `BULK_ADD_TAG` | `Tag all Lambda functions with Project=CloudGuard` · `Add Environment=Development to all DynamoDB tables in ap-south-1` |
| `UPDATE_TAG` | `Update Environment of ServiceWRK to Production` · `Update the Owner tag of Lambda function ServiceWRK to BackendTeam` |
| `BULK_UPDATE_TAG` | `Update Owner of all EC2 instances to platform-team` |

**Targets**
- A specific target is `[the] [type] NAME|ARN`, optionally followed by `in <region>` and/or `in account <id>`.
- A bulk target is `all <type> [in <region>]`.
- Types: `lambda function`, `dynamodb table`, `s3 bucket`, `ec2 instance`.
- Values containing commas, "and", "to" or "with" must be double-quoted.

**Rejected rather than guessed**
- Tag removal or deletion.
- A plural type without "all" (`Tag Lambda functions with ...`).
- `all resources`, or a bulk target without a type.
- A type given without a name.
- Duplicate keys, malformed `KEY=VALUE` pairs, conflicting regions.
- Text the parser doesn't recognise.

## Resolution
- **Names are case-sensitive.** A name that only matches when case is ignored returns `NEEDS_CLARIFICATION` with candidates, never an automatic pick.
- **Several exact matches** (the same name across types or regions) return `NEEDS_CLARIFICATION` with the candidates.
- **Specific requests never widen** into all resources of a type.
- **Bulk requests** return every resource of the stated type and region, de-duplicated by ARN.
- **Out of scope:** another account, or a region that wasn't discovered, is rejected as `OUT_OF_SCOPE`.

## Per-resource outcomes
Every considered resource becomes one `ResourceRecord` with exactly **one** outcome. Validators reject a record whose details don't match its outcome.

| Outcome | Meaning | Details carried |
|---|---|---|
| `CHANGE_PROPOSED` | At least one tag would be added, or overwritten by an update | `changes` (ADD, or OVERWRITE with `old_value`); may also carry `matching`/`conflicts` for other keys |
| `ALREADY_MATCHING` | Every requested tag already has the requested value | `matching` |
| `CONFLICT` | Nothing to change, and at least one tag conflicts | `conflicts` with reason `WOULD_OVERWRITE`, `KEY_NOT_PRESENT`, `KEY_CASE_CONFLICT` or `TAG_LIMIT_EXCEEDED` |
| `POLICY_VIOLATION` | A requested value breaks the tagging policy for this resource type | `policy_violations` (allowed values and aliases); nothing proposed for this resource |
| `INSPECTION_FAILED` | Current tags couldn't be read, so no change can be proven safe | `inspection_error` |
| `OUT_OF_SCOPE` | Bulk only: same type, but excluded by the request's region | `out_of_scope_reason` |

Overwrite protection by request type:

| Situation | ADD / BULK_ADD | UPDATE / BULK_UPDATE |
|---|---|---|
| Key absent | change `ADD` | conflict `KEY_NOT_PRESENT` (never silently added) |
| Same value present | matching | matching |
| Different value present | conflict `WOULD_OVERWRITE` | change `OVERWRITE` with `old_value`, plus `requires_overwrite_confirmation` |
| Key present in another case (`project` vs `Project`) | conflict `KEY_CASE_CONFLICT` | conflict `KEY_CASE_CONFLICT` |
| Would exceed 50 user tags | conflict `TAG_LIMIT_EXCEEDED` | n/a |

Every inspected record carries the resource's **complete** `current_tags` exactly as read. Nothing is ever removed.

## Impact summary
`proposal.impact` (an `ImpactSummary`) is **computed from the records** by `summarize_records()`. The proposal validator recomputes it and rejects any mismatch. It contains:
- **Counts per outcome:** matched (in scope), changing, already matching, conflicting, policy violations, inspection failures, out of scope, plus changing resources that also have partial conflicts.
- **Tag-level counts:** added, overwritten, conflicts (also broken down by reason), policy violations.
- `resource_arns_by_outcome`: every outcome key is always present, even when empty.
- `proposed_changes`: a flat list of `{resource, key, action, old_value, new_value}`.

## Validation
- **AWS constraints** (the rules common to Lambda, DynamoDB, S3 and EC2):
  - Keys are 1–128 characters with no leading or trailing spaces, and can't start with `aws:`.
  - Values are at most 256 characters.
  - Allowed characters are letters, digits, spaces and `_ . : / = + - @`.
  - At most 50 user tags per resource.
- **Request-level rejection:** violating any AWS constraint, or giving a blank value, rejects the whole request (`REJECTED`), because CloudGuard never invents values.
- **Policy:** if the key is governed by the policy for the resource's type, the value must be allowed. Otherwise the record is `POLICY_VIOLATION`, and the proposal is `NOT_APPROVABLE`, with outcome status `BLOCKED`. Aliases are accepted with a note giving the canonical value, but never rewritten. A custom key is labelled `CUSTOM` and never becomes mandatory.

## Proposal integrity and approvability
- `content_hash` covers every field except itself. Validators reject:
  - a hash that doesn't match;
  - an impact summary not derived from the records;
  - duplicate resources, or a record from another account;
  - an overwrite inside an add operation;
  - an overwrite flag that doesn't match the changes;
  - a specific proposal touching more than one resource;
  - an approval status that doesn't match approvability;
  - a TTL outside (0, 1 hour].
- `approval_status` is `PENDING` only when at least one resource changes and there are no policy violations. Otherwise it's `NOT_APPROVABLE`, with `blocking_reasons`.
- See [approval-readiness.md](approval-readiness.md) for the fields an approval must bind to, and for the revalidation required before execution (Phase 4).

| Outcome status | When |
|---|---|
| `PROPOSAL_READY` | Approvable proposal |
| `NO_CHANGES` | Proposal shows nothing would change: everything already matches, conflicts or failed inspection |
| `BLOCKED` | Proposal contains policy violations |
| `NEEDS_CLARIFICATION` / `NOT_FOUND` | Target ambiguous, case-only match, or no match |
| `REJECTED` | Unparseable, unsupported, invalid tag, or out-of-scope account or region |

## CLI
```bash
python -m cloudguard.governance tag "Tag all Lambda functions with Project=CloudGuard"            # fixture
python -m cloudguard.governance tag "Update Owner of billing-api to BackendTeam" --mode live --region ap-south-1
```
Exit codes: 0 means a proposal is ready or there are no changes, 1 means clarification needed or not found, 2 means rejected, 3 means blocked by a policy violation.
