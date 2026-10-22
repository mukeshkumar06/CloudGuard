# Tag recommendations (`recommend_tags`)

**Read-only.** `recommend_tags` explains which tags one resource is missing (or holds invalid values for), and suggests a value **only where evidence supports one**. It never invents a value. It writes no tag, creates no proposal, approves nothing and executes nothing.

- **Engine:** `backend/cloudguard/governance/recommendations.py`. It is deterministic and pure: no AWS call, no LLM, no write.
- **MCP tool:** `recommend_tags` in `backend/cloudguard/server/governance_tools.py`. It is the 11th tool (6 cost, 5 governance) and needs the `governance:read` scope in the deployment prototype.
- **Origin:** adapted from CloudGuard Pro's `recommendations/recommend.py`, covering evidence precedence, the value and source gates, and rejection of unmatched or foreign-account scopes. It is rebuilt on CloudGuard AI's `TagFinding` and versioned `TagRule`. The policy model is unchanged.

## Input
One argument, `resource`. It must be one of:
- a `finding_id` (`tagcomp-` + 16 hex digits) from `list_untagged_resources` or `scan_tag_compliance`;
- the ARN of a Lambda function, DynamoDB table, S3 bucket or EC2 instance.

Any other shape is a validation error, and any other argument is refused (`does not accept`). The tool looks the resource up in the latest scan, which is reused while it's younger than 5 minutes.

The tool refuses:
- an ARN whose account differs from the server's own account (`belongs to a different AWS account`);
- an ARN or finding ID that isn't in the scan (`not in the latest scan`).

Neither error echoes the ARN back.

## Evidence hierarchy
For each policy key that is **missing or invalid** on the resource (keys already valid are listed in `verified_tags` and get nothing), CloudGuard checks these sources in order:

1. **Existing valid tags on the same resource.** A valid key is already done; nothing is recommended for it.
2. **Approved key aliases on the same resource.** These are fixed in code (`KEY_ALIASES`):

   | Policy key | Alias tag keys |
   |---|---|
   | Environment | `env`, `stage`, `environ` |
   | Project | `app`, `application`, `project-name`, `project_name` |
   | Owner | `team`, `owner-team`, `owning-team`, `contact`, `maintainer` |
   | CostCenter | `cost-center`, `cost_center`, `costcentre`, `cost-centre` |

   The alias value must pass the value gate and the **effective policy for the resource type** (per-type overrides included). The policy's own value aliases normalise it, for example `env=prod` becomes `Production`. Ambiguous keys such as `service`, `product`, `billing` and `cost` are deliberately not aliases.
3. **Explicit operator-provided values with recorded provenance.** These come from the operator evidence file (below): an exact-ARN entry, otherwise the explicit account-wide scope `account:<ACCOUNT_ID>`. An exact entry **supersedes** a default for the same key; the default is still listed as evidence, with `accepted: false` and reason "superseded by the exact-resource operator entry". Owner can never have an account-wide default: ownership is per resource.
4. **Otherwise `NEEDS_USER_INPUT`.**

Every source that yields a valid value is collected; sources never override each other.
- One distinct value gives `RECOMMENDED`.
- Two different values give `CONFLICT`.
- An explicit operator value that fails validation gives `INVALID_EVIDENCE` and is never replaced by an alias.

### Never evidence
- **Creator or tool tags** (`created-by`, `createdBy`, `created_by`, `creator`, `aws:createdBy`). They say who created a resource, not who is responsible for it. For Owner they are recorded as rejected evidence, with reason code `CREATOR_TAG_IGNORED`.
- **Resource names, ARNs, `Name`/`Description`/`Notes` tags and any other metadata.** CostCenter is never inferred from a name such as `cc-1042-ledger`.
- **The current invalid value itself.** It is reported in `current_value`, but never recommended back.

## Statuses and reason codes
| `status` | `reason_code` | Meaning |
|---|---|---|
| `RECOMMENDED` | `SUPPORTED_BY_EVIDENCE` | Exactly one distinct value, valid under the effective policy. |
| `NEEDS_USER_INPUT` | `NO_EVIDENCE` | Nothing to go on. |
| `NEEDS_USER_INPUT` | `CREATOR_TAG_IGNORED` | Only a creator tag exists (Owner). |
| `CONFLICT` | `CONFLICTING_EVIDENCE` | Valid sources disagree. A person chooses. |
| `INVALID_EVIDENCE` | `INVALID_ALIAS_VALUE` | The only evidence on the resource failed the value gate or policy. |
| `INVALID_EVIDENCE` | `INVALID_OPERATOR_VALUE` | The operator value or its `source` failed validation. |
| `INVALID_EVIDENCE` | `OPERATOR_EVIDENCE_REJECTED` | The configured operator file is unusable, so **nothing** is recommended: the rejected file might have contradicted the other evidence. |

Every non-`RECOMMENDED` result carries `human_input_reason`, and `recommended_value` is `null`.

## Confidence limitations
`confidence` is a **fixed label per evidence type, not a probability**:
- `medium`: a value from the same resource, or an operator entry for that exact resource;
- `low`: only the account-wide operator default. Such a result also carries `human_input_reason` ("not verified for this resource; a person must confirm it"), and speech says the key "only has an account-wide default, which a person must confirm";
- `none`: nothing is recommended.

CloudGuard does not verify that a value is *true*. An alias tag can be stale, and an operator sheet can be wrong. `limitations` in every response says this.

## Value and source gates
- **Value** (`validate_tag_value`): text, non-blank, no leading or trailing whitespace, at most 256 characters, and only the characters AWS tags allow (letters, digits, spaces and `_ . : / = + - @`). Placeholders are rejected: `TBD`, `unknown`, `N/A`, `none`, `changeme`, `-`, `pending`, and similar. Free-form values that read as text rather than a tag are rejected with an explicit reason: more than 6 words; instruction-like words (`ignore`, `disregard`, `instruction(s)`, `assistant`, `prompt`, `override`, `previous`, `pretend`, `jailbreak`, matched as whole words); or embedded `key=value` assignments in a multi-word value. These gates apply to alias tags and operator values alike. Legitimate values such as `payments-team`, `billing-system`, `CC-1042` or `a=b` still pass. The value must then match the rule's allowed values or value aliases, when the rule has any.
- **Source** (`validate_source`): 3 to 80 characters with at least one letter or digit, and no control, line-break or invisible (bidi) characters. Placeholders (`TBD`, `example`, `<source>`, `[ticket]`, `to be determined`, ...) are rejected. A source that is too long is rejected, never truncated.

## Operator evidence file
The server operator configures this file at startup. **It is never a tool argument**, so an MCP caller can't supply values or point at a file.

```bash
python -m cloudguard.server --mode fixture --operator-evidence fixtures/operator-evidence-demo.json
```

```json
{
  "version": 1,
  "scopes": {
    "arn:aws:lambda:ap-south-1:123456789012:function:legacy-report-generator": {
      "Owner": {"value": "reporting-team", "source": "OPS-1234 service ownership review"}
    },
    "account:123456789012": {"CostCenter": {"value": "CC-1000", "source": "Finance cost-allocation sheet 2026-Q4"}}
  }
}
```

- **Loading:** the path is resolved to an absolute path at startup, and the JSON shape is checked then (the server refuses to start on a malformed file). The file is read, never written, on each `recommend_tags` call, so edits apply without a restart. At most 1 MB.
- **Whole-file rejection:** any structural problem rejects the whole file. That includes:
  - a repeated JSON key;
  - a version other than 1, or extra top-level fields;
  - a scope that is neither `account:<ACCOUNT_ID>` nor a supported resource ARN (a bare `"*"` is refused: the account-wide scope must be written explicitly);
  - an account-wide scope naming another account, or an account-wide Owner;
  - an ARN of another account;
  - an ARN that matches no resource in the current scan (including one in a region whose listing failed);
  - a key the policy doesn't define;
  - a key given twice in different case;
  - an entry that isn't exactly `{value, source}`.

  The response's `operator_evidence.problems` lists the problems by service and resource name only (no account, region, values or sources).
- **Provenance:** every accepted operator value carries its `source` in `evidence[].reference`. `operator_evidence.content_hash` is the SHA-256 of the exact file bytes used.
- **Status note:** `operator_evidence.note` states in plain words what was used. With no file configured, the status is `not_configured` and the note says only tags on the resource were considered. The note never contains a path.
- **Not yet wired in:** the Lambda deployment prototype (`cloudguard.deploy`) doesn't pass an operator file, so there the status is always `not_configured`, with that note. A test asserts this.

## Security restrictions
- **Untrusted data:**
  - Tag keys, tag values, resource names and operator sources are untrusted data. They are only compared as data, so prompt-like text (for example the demo fixture's `Notes` tag) can't change what the engine does.
  - An alias or operator value containing such text is rejected by the value gate (`INVALID_EVIDENCE`), even on free-form keys. The gate is a deterministic heuristic, not a guarantee; anything recommended still needs an approved proposal.
- **Speech** names a resource only by service, region and finding number, and mentions only policy keys and statuses. It never contains resource names, ARNs, the account ID, tag values, recommended values or sources.
- **No authorization:** `changes_made` and `authorizes_change` are always `false`, and `next_step` says that applying a value needs a separate tagging proposal approved by an authorized person. The response carries no proposal ID, approval ID, content hash or nonce.
- **Read-only:**
  - The tool is annotated `readOnlyHint: true`, `destructiveHint: false`.
  - In live mode its only AWS calls are the shared scan's `List*`, `Describe*` and `Get*` operations, and tests assert this.
  - It writes no file, no policy and no proposal.

## Example responses
Fixture mode, with `fixtures/operator-evidence-demo.json`. `freshness`, `rules`, `policy` and `limitations` are omitted here.

**`legacy-report-generator` (no tags; operator evidence):**
```json
{
  "tool": "recommend_tags", "outcome": "findings", "compliance_status": "NON_COMPLIANT",
  "speech": "For the lambda resource in ap-south-1 (finding 2 of 8): CloudGuard recommends a value for Project, Owner from evidence. CostCenter only has an account-wide default, which a person must confirm. Environment needs a person to provide the value. Nothing was changed.",
  "verified_tags": [],
  "recommendations": [
    {"key": "Environment", "status": "NEEDS_USER_INPUT", "reason_code": "NO_EVIDENCE", "recommended_value": null,
     "confidence": "none", "evidence": [],
     "policy_validation": {"allowed_values": ["Development", "Staging", "Production"], "value_valid": null, "reason": "no value to check"},
     "human_input_reason": "There is no evidence for this value; a person responsible for the resource must provide it."},
    {"key": "Owner", "status": "RECOMMENDED", "reason_code": "SUPPORTED_BY_EVIDENCE", "recommended_value": "reporting-team",
     "confidence": "medium",
     "evidence": [{"source_type": "operator_exact", "reference": "OPS-1234 service ownership review",
                   "observed_value": "reporting-team", "normalized_value": "reporting-team", "accepted": true, "reason": "ok"}],
     "policy_validation": {"allowed_values": [], "value_valid": true, "reason": "ok"}, "human_input_reason": null},
    {"key": "CostCenter", "status": "RECOMMENDED", "recommended_value": "CC-1000", "confidence": "low",
     "human_input_reason": "Account-wide operator default, not verified for this resource; a person must confirm it applies before it is used.",
     "evidence": [{"source_type": "operator_default", "reference": "Finance cost-allocation sheet 2026-Q4", "accepted": true}]}
  ],
  "status_counts": {"RECOMMENDED": 3, "NEEDS_USER_INPUT": 1, "CONFLICT": 0, "INVALID_EVIDENCE": 0},
  "operator_evidence": {"status": "loaded", "entries": 2, "content_hash": "sha256:…", "problems": [], "note": "Operator evidence file loaded; each accepted value names its source."},
  "changes_made": false, "authorizes_change": false,
  "next_step": "Nothing was changed. To apply a value, a person must create a tagging proposal and an authorized approver must approve it; this result is not an approval."
}
```

**`batch-worker-1` (EC2: blank `Owner`, a prompt-like `Notes` tag):**
```json
{
  "speech": "For the ec2 resource in ap-south-1 (finding 5 of 8): Owner needs a person to provide the value. Nothing was changed.",
  "verified_tags": ["Environment", "Project", "CostCenter"],
  "recommendations": [{"key": "Owner", "status": "NEEDS_USER_INPUT", "reason_code": "NO_EVIDENCE",
                       "current_value": "   ", "evidence": [], "recommended_value": null}]
}
```

**Alias and creator tags** (`env=prod`, `app=checkout`, `created-by=jdoe@example.com`): Environment is `RECOMMENDED` as `Production` (alias normalised by the policy), and Project as `checkout`. Owner is `NEEDS_USER_INPUT` / `CREATOR_TAG_IGNORED`. Adding an operator default `Project=billing` turns Project into `CONFLICT`.

## Tests
`backend/tests/test_tag_recommendations.py`: the engine (hierarchy, gates, never-evidence, operator file) and the MCP tool (arguments, refusals, schema, speech, read-only behaviour, the existing ten tools unchanged).
