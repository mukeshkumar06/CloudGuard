# Tagging policy

A policy is JSON with **account-level defaults** plus optional **resource-type overrides**. It is validated in full before it can be saved, and every save creates a new immutable version.

```json
{
  "defaults": [
    {
      "key": "Environment",
      "description": "Deployment environment",
      "allowed_values": ["Development", "Staging", "Production"],
      "aliases": {"dev": "Development", "stage": "Staging", "prod": "Production"}
    },
    {"key": "Project"},
    {"key": "Owner"},
    {"key": "CostCenter"}
  ],
  "overrides": {
    "AWS::EC2::Instance": {
      "rules": [{"key": "PatchGroup"}],
      "remove": ["CostCenter"]
    }
  }
}
```
That is the built-in default policy (`policy_id = "default"`), plus an example override.

## Rule fields
| Field | Meaning |
|---|---|
| `key` | Tag key, matched **case-insensitively**. 1–128 characters; must not start with `aws:`. |
| `required` | Default `true`. If `false`, the tag may be absent, but a present value must still be valid. |
| `allowed_values` | Canonical values, matched case-insensitively. Empty means any non-blank value. |
| `aliases` | Maps an alternative value to a canonical one. Requires `allowed_values`. Targets are stored in canonical spelling. |

## Overrides
Overrides are allowed only for `AWS::Lambda::Function`, `AWS::DynamoDB::Table`, `AWS::S3::Bucket` and `AWS::EC2::Instance`.

The effective rules for a resource type are built in this order:
1. Start from `defaults`.
2. Drop the keys listed in `remove`.
3. Replace any default rule whose key matches an override rule (case-insensitive). The replacement keeps the default's position.
4. Append the remaining override rules.

Types without an override inherit the defaults unchanged.

## Evaluation results
| Situation | Status effect | Recorded as |
|---|---|---|
| Required key absent | NON_COMPLIANT | `missing_tags` |
| Key present, value blank or whitespace | NON_COMPLIANT | `invalid_tags` (`EMPTY_VALUE`) |
| Value not allowed and not an alias | NON_COMPLIANT | `invalid_tags` (`VALUE_NOT_ALLOWED`) |
| Value is an alias, or differs from the canonical value only by case | none (valid) | notice `NON_CANONICAL_VALUE`, carrying the canonical value |
| Key differs from the policy key only by case | none | notice `KEY_CASE_MISMATCH` |
| Several keys differ only by case (`Owner` and `owner`) | none if any one is valid | notice `DUPLICATE_KEY_CASE` |
| Tags couldn't be read | INSPECTION_FAILED | `inspection_error` |

**Notices are advisory.** CloudGuard never renames keys or rewrites values on its own. A canonical value only reaches AWS through a proposal the user explicitly approves (Phase 4).

## Validation (rejected before saving)
A policy is rejected before saving if any of these hold:
- **Empty defaults:** no default rules at all.
- **Duplicates:** duplicate keys, allowed values or aliases, compared case-insensitively.
- **Blank or oversized entries:** blank keys or values; keys over 128 characters; values over 256; `aws:` keys.
- **Bad aliases:**
  - Aliases defined without `allowed_values`.
  - An alias targeting a value outside `allowed_values`.
  - An alias that collides with an allowed value.
- **Bad overrides:**
  - An unsupported resource type.
  - Removing a key that isn't in the defaults.
  - Overriding and removing the same key.
  - An override that leaves no mandatory tags.
- **Unknown fields:** any field not described above.

## Optional tags
- `required: false` tags are never reported as missing and are excluded from `required_tags`.
- If an optional tag is present, its value is still validated: blank values and values outside the allow list are violations. They're reported in `invalid_tags` with `mandatory: false` and make the resource NON_COMPLIANT.
- A per-type override can make a default tag optional for one resource type only.

## Three separate result categories
| Category | Fields | Affects status? |
|---|---|---|
| Violations | `missing_tags`, `invalid_tags` | Yes. They are the only cause of NON_COMPLIANT. |
| Notices | `notices` | Never. Compliant and non-compliant resources can both carry them. |
| Inspection failure | `inspection_error` | Status is INSPECTION_FAILED, with no violations or notices. |

These rules are enforced by validation on `TagFinding`, and counted separately in `ScanSummary` (`total_violations`, `total_notices`, `inspection_failed_resources`). `scan --output summary` prints each category in its own section.

## Versioning
- Each save produces `(policy_id, version, content_hash)`. The hash is SHA-256 over canonical JSON, with fields left at their defaults excluded. That way, adding a defaulted field to the schema later doesn't change the hashes of stored versions.
- Saves use optimistic concurrency: the caller passes the active version it is replacing. A stale value is rejected, and so is an unchanged document.
- `restore N` re-saves an old document as a new version with `restored_from: N`, so history stays linear. A scan under the restored version records the *new* version number.
- Every scan records `policy = {policy_id, version, content_hash}`. `PolicyManager.resolve(ref)` returns exactly that immutable version, and fails if the stored hash differs. Stored versions are also re-verified against their hash whenever they're loaded, so a tampered or corrupted version can't silently change a historical result. Re-running a scan with `--policy-version N` reproduces its original compliance results, even after later restores and edits.

```bash
python -m cloudguard.governance policy validate my-policy.json
python -m cloudguard.governance policy history
python -m cloudguard.governance policy save my-policy.json --expected-version 1 --author alice --note "EC2 patching"
python -m cloudguard.governance scan --mode fixture --policy-version 1
```
