# CloudGuard AI: Friction Log

Developer experience with Amazon and AWS tools while building CloudGuard AI, for the Amazon Developer Hackathon submission.

**Ground rules for this log**
- Every entry is backed by code, tests or documentation in this repository.
- Design decisions are kept separate from real friction.
- Anything that needs a live AWS run to confirm is marked **Not yet verified**. We don't claim that untested features fail.
- No account IDs, credentials or real resource names appear here.

*Last updated: 2026-10-23 (new entries FL-017 to FL-023 for voice, conversational tagging, region clarification, incomplete discovery and an open backend test failure; "Update 2026-10-23" reconciliation notes in §1, §2, FL-015, FL-016 and §5–§7). Earlier: 2026-10-21 (Cost Explorer live-run corrections to §1, §2, FL-007, FL-009 and §7; new entries FL-013 to FL-016). Entries are kept as recorded on their dates; later status changes are marked "Update <date>". Keep it current; see "Maintaining this log" at the end.*

---

## 1. Project context
CloudGuard AI is an agentic AWS governance assistant. It inspects an account with deterministic, read-only checks, reports tagging and cost findings with raw AWS evidence, and proposes tag fixes that a human must approve. It targets the Alexa+ track (a self-hosted MCP server) and the AWS Builder mini challenge.

**What exists so far**
- **Tag governance:** read-only, multi-region resource discovery (Lambda, DynamoDB, S3, EC2), compliance checks and versioned tagging policies.
- **Tag-change proposals:** natural-language requests become proposals, with an approval workflow and a **fixture-only** executor that writes to a simulated account.
- **Cost intelligence:** last month's bill and cost anomalies, read-only.
- **Built after the 2026-10-16 entries (status as of 2026-10-21):**
  - a FastMCP server with twelve tools (six cost, five governance, one identity), all annotated read-only. What "read-only" covers:
    - **AWS:** every tool makes only read calls (Cost Explorer, discovery, STS `GetCallerIdentity`); tests check the annotations and that the server code contains no AWS write API (`backend/tests/test_mcp_cost_tools.py`, `test_mcp_governance_tools.py`, `test_account_identity.py`);
    - **application state:** no tool creates, approves or executes a proposal, or saves a policy; the only state a tool changes is the in-memory scan cache (FL-015);
    - **not MCP tools:** proposals, approvals, the allow-list and the fixture-only executor are CLI-only (`python -m cloudguard.governance`). They do write local or DynamoDB application state; the executor writes tags only to a **simulated** account, and there is no live AWS tag writer;
    - **not deployed:** all of this runs locally;
  - an HTTP deployment prototype with static bearer tokens and an Alexa-scoped surface (not deployed);
  - a browser simulator that is a real MCP client, with voice input and output through the Web Speech API.
- **Not built yet:** the Bedrock agents. (As of 2026-10-16, the MCP server and the voice client were not built either.)
- **Update 2026-10-23 (checked against the code at commit `bdbf8cc`).** Some of the 2026-10-21 statements above are no longer accurate. They are kept for history; this is what changed:
  - **Fourteen MCP tools, not twelve:** six cost, seven governance (`get_resource_inventory`, `scan_tag_compliance`, `search_resources`, `preview_tag_changes`, `list_untagged_resources`, `explain_finding`, `recommend_tags`) and one identity. All are still annotated read-only (`readOnlyHint=True`, `destructiveHint=False`), and no MCP tool writes to AWS or saves, approves or executes a proposal. `preview_tag_changes` is a dry run.
  - **A live AWS tag writer now exists** (`backend/cloudguard/governance/remediation/live_writer.py`): Lambda `TagResource`, DynamoDB `TagResource`, EC2 `CreateTags` (instances only) and S3 `PutBucketTagging` (read-merge-write). It is reachable in two ways only:
    - the CLI, `proposal execute --mode live`, after a *different* person approved the content hash (`docs/live-tagging.md`);
    - **Personal Voice Tagging Mode**, an opt-in HTTP path under `/operator` for a single-operator development account (`backend/cloudguard/voice_tagging/`, `docs/voice-tagging.md`). It is off by default, needs a separate operator credential, and deliberately waives separation of duties for allow-listed accounts only.
  - So "the executor writes only to a simulated account" and "approvals are CLI-only" are out of date: the fixture executor still exists, approval in the voice mode is done server-side for the operator session, and team accounts still need the CLI's two-person flow.
  - **The browser client is no longer fixture-only by design:** it reports Fixture or Live AWS from the server's `/healthz`. It has only been run against the fixture backend in tests; see FL-023 for the verification categories.
  - **All live tagging is tested offline only** (botocore Stubber, `FakeAws`, the simulated account). **No live AWS write has been performed or verified.**
  - **Frontend redesign (2026-10-23):** an eight-page application shell (Overview, Cost Intelligence, Resource Governance, Voice Assistant, Tag Management, Activity & Audit, Settings, MCP Explorer) with a new connect screen, a collapsible sidebar, MCP, mode, region, operator and kill-switch indicators, and responsive breakpoints (`web/src/components/Shell.tsx`, `ConnectScreen.tsx`, `web/src/pages/`). The pages call real MCP tools through `useToolData` (`web/src/ui/data.ts`); none of them shows hard-coded demo data. This is product work rather than AWS friction, so it has no FL entry.

**How things were tested**
- Almost all testing is offline, using botocore `Stubber` and `moto`.
- Live AWS use so far has been read-only, with a developer identity:
  - **resource discovery** (including a governance scan over 18 regions on 2026-10-20);
  - **one reported run of the cost CLI against Cost Explorer on 2026-10-16** (`GetCostAndUsage`, `GetAnomalyMonitors`, `GetAnomalies`). Its raw output was not retained; the observations are recorded in `docs/cost-intelligence.md` ("Unresolved assumptions") and in the regression fixture they led to.
- **Nothing has been deployed.**

## 2. AWS tools and services used
| Tool / service | How we used it | Exercised against real AWS? |
|---|---|---|
| boto3 / botocore (Python SDK) | Every AWS call, standard retry mode, paginators, `Stubber` for offline tests | Yes (discovery) |
| AWS STS `GetCallerIdentity` | Verified account ID for discovery; authenticated caller identity for approvals | Yes (discovery) |
| Lambda, DynamoDB, S3, EC2 read APIs | Multi-region tag discovery | Yes, except EC2 (no instances) |
| Amazon DynamoDB | Single-table store for policies, proposals, approvals, allow-list and execution audit (conditional `TransactWriteItems`) | Offline only (moto); the table was confirmed absent with a read-only `DescribeTable` |
| AWS IAM | Least-privilege example policies; Phase 4 tagger-role design | Design only, nothing attached |
| AWS Cost Explorer and Cost Anomaly Detection | Bill, month-to-date spend, comparison and anomaly reports (read-only) | Yes, once: a cost CLI run on 2026-10-16 (raw output not retained). `GetDimensionValues` was never run. The MCP server has **not** called it live |
| AWS CloudFormation | `infra/cloudguard-core.yaml`, `infra/demo-seed.yaml` | Not deployed |
| AWS Budgets | Optional budget in the core template | Not deployed |
| Amazon Bedrock, Strands Agents | Planned for explanations (Nova Lite for development) | Not started (postponed) |
| MCP Python SDK (`mcp` 1.x, FastMCP) | Server with twelve read-only tools (fourteen as of 2026-10-23; protocol 2025-11-25, stateless Streamable HTTP); "Not started" as of 2026-10-16 | Offline and fixture mode only; the MCP server has **not** been run against live AWS |
| MCP TypeScript SDK, browser Web Speech API | Simulator client (`web/`) and its voice input and output | No AWS involved; fixture backend only. **Update 2026-10-23:** Speech APIs are faked in tests; no real-microphone run is recorded (FL-017, FL-018) |
| Lambda/DynamoDB/EC2/S3 tagging APIs, STS `AssumeRole` | Live tag writer and Personal Voice Tagging Mode (added after 2026-10-21) | No. Offline only (Stubber, fakes); **no live write has been performed** |
| Kiro | Project steering files only (`.kiro/steering/`) | Not an AWS runtime service; no friction recorded |
| Kiro Crew | Not used | — |
| AWS Lambda Web Adapter | HTTP deployment prototype (`backend/deploy/lambda/`) | No: no image build, no deployment; tests drive the ASGI app directly |
| Alexa+ | Target surface for the MCP server | No: no access (see FL-016); a browser MCP client stands in |

## 3. Friction entries
Severity reflects the impact on this project, not a general judgement of the service.

### FL-001: S3 reports "no tags" as an error (`NoSuchTagSet`)
- **Tool/service:** Amazon S3 `GetBucketTagging` (boto3)
- **Stage:** Development
- **What we tried:** telling an untagged bucket apart from a bucket whose tags we aren't allowed to read.
- **Expected behaviour:** an empty `TagSet` with HTTP 200, as Lambda `ListTags` and DynamoDB `ListTagsOfResource` return for untagged resources.
- **Actual behaviour:** `ClientError` with code `NoSuchTagSet` (HTTP 404) for a bucket that has never been tagged.
- **Impact:** Medium. Handled naively, untagged buckets look like "inspection failed", or real failures look like "untagged". Either way the governance results are skewed.
- **Workaround or resolution:** treat `NoSuchTagSet` as an empty tag set; every other error code becomes an inspection failure (`backend/cloudguard/governance/discovery/s3_discovery.py`). Covered by Stubber tests.
- **Amazon improvement suggestion:** return an empty `TagSet` with HTTP 200, or call out this special case prominently in the `GetBucketTagging` and boto3 docs.
- **Status:** Resolved (workaround)

### FL-002: Finding a bucket's region needs a fallback with legacy encodings
- **Tool/service:** Amazon S3 `ListBuckets` (`BucketRegion`) and `GetBucketLocation`
- **Stage:** Development
- **What we tried:** a regional scan that returns only that region's buckets.
- **Expected behaviour:** one consistent region value per bucket.
- **Actual behaviour:**
  - `ListBuckets` is a global call. We filter with `BucketRegion` and read each bucket's `BucketRegion`, but we still need a fallback for buckets that come back without one.
  - The fallback, `GetBucketLocation`, returns `None` (or `""`) for us-east-1 and the legacy value `"EU"` for eu-west-1, so it has to be mapped explicitly.
  - If both calls fail, the bucket's region is unknown. It is reported once as `INSPECTION_FAILED` with region `unresolved`, and de-duplicated by ARN across regional passes.
- **Impact:** Low. A few lines of special-casing, but it's easy to get wrong silently.
- **Workaround or resolution:** an explicit mapping in `s3_discovery.py` (`{None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}`) and ARN de-duplication across regions. Covered by stubbed tests.
- **Amazon improvement suggestion:** document whether `BucketRegion` is always present in `ListBuckets` responses. Have `GetBucketLocation` return canonical region codes, with an opt-in for the legacy values.
- **Status:** Workaround. The fallback path is **Not yet verified** live: our live runs always returned `BucketRegion`.

### FL-003: `s3:PutBucketTagging` can't be restricted by tag key, and it replaces the whole tag set
- **Tool/service:** AWS IAM with Amazon S3 tagging
- **Stage:** Development (IAM design)
- **What we tried:** a least-privilege role that may only add or overwrite specific, approved tag keys on buckets, in the same way we can for Lambda, DynamoDB and EC2.
- **Expected behaviour:** `aws:TagKeys` and `aws:RequestTag/${key}` condition keys, as `lambda:TagResource`, `dynamodb:TagResource` and `ec2:CreateTags` support.
- **Actual behaviour:** these are three behaviours that we confirmed in AWS documentation; none of them was observed in a live run.
  - According to the Service Authorization Reference (checked 2026-10-14), `s3:PutBucketTagging` supports neither `aws:TagKeys` nor `aws:RequestTag`.
  - According to the S3 API reference, `PutBucketTagging` **replaces the entire tag set**.
  - The same permission also authorizes `DeleteBucketTagging`.
  - So a principal allowed to tag one bucket can wipe or rewrite every tag on it.
- **Impact:** High for a tool whose only write action is approved tagging. IAM alone can't enforce "add only these keys" on S3.
- **Workaround or resolution:** a design workaround (`docs/phase4-iam-design.md`):
  - read, merge and write the full tag set;
  - revalidate the *whole* bucket tag set before writing and verify it after;
  - scope sessions to exact bucket ARNs;
  - deny protected buckets via `aws:ResourceTag`.
- **Amazon improvement suggestion:** support `aws:TagKeys` and `aws:RequestTag` on `s3:PutBucketTagging`, or add an additive "merge tags" S3 API with its own IAM action, separate from delete.
- **Status:** Workaround (design). **Not yet verified** live: no tagger role exists yet.

### FL-004: Case-insensitive tag-key denies aren't possible for prefixes
- **Tool/service:** AWS IAM condition operators
- **Stage:** Development (IAM design)
- **What we tried:** denying writes to reserved or ABAC-sensitive tag keys in any letter case, for example the `cloudguard:` prefix and protected keys.
- **Expected behaviour:** a case-insensitive pattern match.
- **Actual behaviour:**
  - The IAM docs (checked 2026-10-14) say condition key **names** are case-insensitive, but `StringLike`/`StringEquals` compare **values** case-sensitively, including the key list in `aws:TagKeys`.
  - `StringEqualsIgnoreCase` exists, but there is no `StringLikeIgnoreCase`.
  - So a prefix deny can be bypassed by a case variant, while someone else's ABAC policy keyed on `aws:ResourceTag/team` still matches `TEAM`.
- **Impact:** Medium. A case-variant key could slip past a prefix deny.
- **Workaround or resolution:**
  - list exact protected keys with `StringEqualsIgnoreCase`;
  - spell out common prefix variants;
  - reject reserved keys case-insensitively in the application;
  - allow only exact approved keys in per-approval session policies.
- **Amazon improvement suggestion:** add `StringLikeIgnoreCase`, or document this ABAC pitfall next to the `aws:TagKeys` guidance.
- **Status:** Workaround (design). **Not yet verified** with the IAM policy simulator or Access Analyzer.

### FL-005: Tagging APIs have no conditional write or fencing token
- **Tool/service:** Lambda `TagResource`, DynamoDB `TagResource`, EC2 `CreateTags`, S3 `PutBucketTagging`
- **Stage:** Development
- **What we tried:** making sure an approved tag change is applied only if the resource still has the tags the human approved against, and that a stale executor (one whose lease was taken over) can't write.
- **Expected behaviour:** an optimistic-concurrency option, such as "only if the current value is X" or an ETag or version.
- **Actual behaviour:**
  - None of these tagging APIs offers a condition tied to the current tag state.
  - None accepts a fencing token.
- **Impact:** Medium. There is an unavoidable gap between the last read or ownership check and the write:
  - another actor can change the same key in that instant, and our approved value replaces it;
  - a paused writer can land a write late.
- **Workaround or resolution:**
  - re-read immediately before writing, and verify after;
  - before every write, record a fenced "write intent" with a short write deadline, inside a lease with a safety margin;
  - an executor that loses its lease issues no further writes;
  - any unconfirmed write is surfaced in the audit (`in_flight` / `UNCONFIRMED_WRITE`).
  - Tested offline (`backend/tests/test_execution_safety.py`), against a **simulated** account only.
- **Amazon improvement suggestion:** an optional `ExpectedTags`/`IfMatch`-style condition on tagging APIs, or a resource tag version (ETag) that writes can be conditioned on.
- **Status:** Open (mitigated and bounded, not eliminated). Live behaviour is **Not yet verified**.

### FL-006: No obvious creation-unique identifier for detecting a recreated Lambda function
- **Tool/service:** AWS Lambda
- **Stage:** Development
- **What we tried:** detecting that a resource was deleted and recreated under the same ARN between approval and execution, so an approval can't apply to a different resource.
- **Expected behaviour:** an immutable per-creation ID, like DynamoDB `TableId`, the EC2 instance ID or the S3 bucket creation date.
- **Actual behaviour:** we did not identify an equivalent identifier for Lambda functions. A function recreated with the same name has the same ARN.
- **Impact:** Low to medium. Recreation of a Lambda function can't be detected before a tag write.
- **Workaround or resolution:** Lambda revalidation records an explicit `identity_note` that recreation is undetectable. Tag drift checks still apply (`remediation/revalidation.py`).
- **Amazon improvement suggestion:** expose a stable per-creation identifier for functions, or document the recommended way to detect recreation.
- **Status:** Open. **Not yet verified** whether a suitable field exists in `GetFunction`.

### FL-007: Cost Explorer `RECORD_TYPE` values aren't enumerated in the API reference
- **Tool/service:** AWS Cost Explorer `GetCostAndUsage` (`GroupBy RECORD_TYPE`)
- **Stage:** Development
- **What we tried:** splitting last month's bill into usage, fees, tax, discounts, credits and refunds, so credits never reduce a service's total by accident.
- **Expected behaviour:** a documented, exhaustive list of `RECORD_TYPE` values with their exact API spellings.
- **Actual behaviour:**
  - We found the names only in the console user guide's "Charge type" list.
  - Some discount spellings, for example `EdpDiscount`, `SppDiscount` and `DiscountedUsage`, are only candidates.
- **Impact:** Medium. Unknown values would silently corrupt totals.
- **Workaround or resolution:**
  - an explicit classification table (`backend/cloudguard/cost/record_types.py`), with forgiving matching on case, spaces, hyphens and underscores;
  - an `unclassified` bucket that is never counted as a charge, plus an `UNCLASSIFIED_RECORD_TYPES` warning;
  - reconciliation tests on 200 randomised lines.
- **Amazon improvement suggestion:** publish the full `RECORD_TYPE` value list, with API spellings and sign conventions, in the Cost Explorer API reference.
- **Status:** Workaround. Partly verified.
  - **Update 2026-10-21:** the reported live run of 2026-10-16 returned `Credit`, `Tax` and `Usage` through `GetCostAndUsage` grouped by `RECORD_TYPE`, and the classification table handled them.
  - The other values, including every discount spelling, are still **Not yet verified**: `GetDimensionValues` was never run, and no record of other types from that run was kept.

### FL-008: Anomaly amounts have no currency field
- **Tool/service:** AWS Cost Anomaly Detection (`GetAnomalies`, `Impact`)
- **Stage:** Development
- **What we tried:** reporting the size of an anomaly in a currency we could state with confidence.
- **Expected behaviour:** an amount paired with a currency unit, as `GetCostAndUsage` returns.
- **Actual behaviour:**
  - The `Impact` data type describes its fields as "the dollar value…", but the response carries **no currency field**.
  - AWS documents that `TotalImpactPercentage` is omitted when expected spend is zero.
- **Impact:** Low to medium. The currency has to be asserted from the documentation, not from data.
- **Workaround or resolution:**
  - report `currency: "USD"` with `currency_basis: "documented_not_returned"` and the doc URL;
  - add a `CURRENCY_MISMATCH` warning if the bill currency differs;
  - explain a missing percentage instead of computing one.
- **Amazon improvement suggestion:** return a `Unit` or currency next to every `Impact` amount.
- **Status:** Workaround. **Not yet verified** live.

### FL-009: Cost Explorer prerequisites are hard to confirm up front
- **Tool/service:** AWS Cost Explorer, Billing and Cost Management, IAM
- **Stage:** Setup
- **What we tried:** a read-only Cost Explorer integration, run from a developer IAM identity in an account whose resources live in `ap-south-1`.
- **Expected behaviour:** a short, authoritative checklist of what an IAM identity needs before `ce:*` API calls work.
- **Actual behaviour:**
  - The API has a single `us-east-1` endpoint, whatever region the resources are in. That's documented, but easy to miss.
  - We recorded that our developer identity had no `ce:*` permissions, so the smoke test was pending. **Update 2026-10-21:** that note was wrong or out of date: the same identity's live calls to `GetCostAndUsage`, `GetAnomalyMonitors` and `GetAnomalies` succeeded on 2026-10-16. We have no record of why the earlier note said otherwise.
  - We couldn't confirm from the docs:
    - whether the "IAM user and role access to Billing information" setting gates the CE **API** or only the console;
    - how the documented delay of up to about 24 hours after first enabling Cost Explorer shows up in the API (we expect `DataUnavailableException`, which is documented only as "The requested data is unavailable.");
    - whether CE API requests (about $0.01 each) are charged or refused on the AWS Free plan.
- **Impact:** Medium. It blocked live verification, and it shapes error messages: we report AWS's documented description separately from our *inferred* possible causes, and never claim a cause.
- **Workaround or resolution:**
  - pin the client to `us-east-1`;
  - a written pre-flight checklist (`docs/cost-intelligence.md`) and a separate read-only policy (`infra/iam/cost-intelligence-policy.json`);
  - capped pagination, with a request count reported in every response.
- **Amazon improvement suggestion:** a single "Cost Explorer API prerequisites" page, plus a more specific `DataUnavailableException` reason (not enabled, still preparing, outside retention).
- **Status:** Partly verified (update 2026-10-21).
  - **Verified live on 2026-10-16** (reported run, raw output not retained): the `us-east-1` endpoint worked with the developer identity for all three read calls, and `GetCostAndUsage` accepted a month-to-date request.
  - **Still Not yet verified:** whether the billing-access setting gates the API; how first-enablement delay appears in the API; whether the Free plan charges or refuses CE requests (we have no record of the charge for those calls).

### FL-010: DynamoDB transaction and batch IAM semantics are easy to misread
- **Tool/service:** AWS IAM with Amazon DynamoDB
- **Stage:** Development
- **What we tried:** a least-privilege policy for a store where audit items are append-only and nothing may be deleted.
- **Expected behaviour:** a `dynamodb:TransactWriteItems` action, and per-operation authorization for batch writes.
- **Actual behaviour:**
  - There is no `TransactWriteItems` IAM action. Each transaction entry is authorized as its underlying operation (`PutItem`, `UpdateItem`, `ConditionCheckItem`); AWS documents this (checked 2026-10-14).
  - During our security review, we concluded that `BatchWriteItem` is a single action covering **both** put and delete requests. So "no `DeleteItem` granted" does not mean "no deletes" if `BatchWriteItem` is allowed.
- **Impact:** Medium. An early template comment overstated the protection. It's listed as finding H2 in `HANDOVER.md`, and the template is not to be deployed until that's fixed.
- **Workaround or resolution:**
  - document the per-item transaction rules (`docs/aws-permissions.md`);
  - plan to drop `BatchWriteItem` and scope writes with `dynamodb:LeadingKeys`.
- **Amazon improvement suggestion:** a "least privilege for transactions and batch writes" table in the DynamoDB IAM docs, stating explicitly that `BatchWriteItem` covers deletes.
- **Status:** Open. The `BatchWriteItem` delete coverage and `LeadingKeys` behaviour are **Not yet verified** (Access Analyzer or IAM simulator).

### FL-011: Assumed-role identity can't tell apart people who share a role
- **Tool/service:** AWS STS `GetCallerIdentity`
- **Stage:** Development
- **What we tried:** separation of duties, so the person who requests a change can't approve it.
- **Expected behaviour:** a stable, verified per-person identity in the caller ARN.
- **Actual behaviour:**
  - For assumed roles, the ARN is `assumed-role/<role>/<session>`, and the caller chooses the session name. Only the role is a trustworthy identity.
  - So two people sharing a role look the same, and one person with two principals (an IAM user and a role) looks like two people.
- **Impact:** Medium for approval workflows (finding M3 in `HANDOVER.md`).
- **Workaround or resolution:** separation of duties compares canonical role-level identities, which fails safe for shared roles, and the limitation is documented in `docs/authorization.md`. `sts:SourceIdentity` and Identity Center user IDs are candidates, but not implemented.
- **Amazon improvement suggestion:** clearer guidance on getting a verified per-human identity from `GetCallerIdentity` (`SourceIdentity`, Identity Center) for in-app separation of duties.
- **Status:** Open (design). **Not yet verified** with Identity Center ARNs.

### FL-012: Lambda Web Adapter docs give an image tag but no digest to pin
- **Tool/service:** AWS Lambda Web Adapter (`public.ecr.aws/awsguru/aws-lambda-adapter`), Amazon ECR Public
- **Stage:** Deployment preparation
- **What we tried:** pinning the adapter in the prototype image (`backend/deploy/lambda/Dockerfile`) to an immutable reference instead of `latest`.
- **Expected behaviour:** the README or release notes list the image digest for each release, next to the tag and layer ARN.
- **Actual behaviour:**
  - The README shows `aws-lambda-adapter:1.1.0` and layer version 30, but no digest.
  - Without Docker, the digest has to be read from the ECR Public registry API: an anonymous token from `https://public.ecr.aws/token/`, then the `Docker-Content-Digest` header of `HEAD /v2/awsguru/aws-lambda-adapter/manifests/1.1.0`.
- **Impact:** Low. It's an extra step, and it's easy to stay on a mutable tag.
- **Workaround or resolution:** pinned `1.1.0@sha256:17cfd08e…` (the manifest-list digest, resolved 2026-10-20) and layer `LambdaAdapterLayerX86:30` (`backend/deploy/lambda/Dockerfile`, `run.sh`, `README.md`).
- **Amazon improvement suggestion:** publish per-release image digests (and layer ARNs) in the release notes.
- **Status:** Resolved (workaround). The image build and the adapter itself are **Not yet verified**: there has been no Docker build and no deployment.

### FL-013: Cost Explorer amounts carry floating-point residues that nearly cancel
- **Tool/service:** AWS Cost Explorer `GetCostAndUsage` (`UnblendedCost`, grouped by SERVICE and RECORD_TYPE)
- **Stage:** Live verification (reported run, 2026-10-16)
- **What we tried:** speaking a month's calculated net and attributing a month-over-month change, in an account where credits cover usage.
- **Expected behaviour:** line amounts that sum to an exact cents value when usage is fully offset by credits.
- **Actual behaviour (AWS data):** amounts carry about 10 decimal places, and lines that should cancel leave tiny residues. Recorded from that run: a month's net of `-3.8E-9` and a matching-days net change of `+1.5E-9`. This is an observation of the data, not an AWS error.
- **Actual behaviour (our bug):** our first version spoke the `-3.8E-9` net as "a net credit of 0.00" and classified the `+1.5E-9` change as `INCREASE_ATTRIBUTED`. That was our application's fault: it treated any non-zero value as meaningful.
- **Impact:** Medium. Numerical noise was reported as a cost increase, and a zero-ish net was read out as a credit.
- **Workaround or resolution:**
  - a materiality threshold of one cent (`MATERIALITY_THRESHOLD = 0.01`, `backend/cloudguard/cost/models.py`): smaller changes or contributions are never called increases, decreases or drivers, and are totalled in `below_threshold_change`;
  - a speech tolerance (`SPEECH_NET_TOLERANCE = 0.005`, `cost/voice.py`): a net below it is spoken as "approximately zero";
  - billing figures are never rounded or suppressed; JSON keeps exact amounts;
  - regression tests and a synthetic `near-zero-net` fixture (`backend/tests/test_cost_materiality.py`), offline only.
- **Amazon improvement suggestion:** document the precision of Cost Explorer amounts, and note that lines which offset each other may not sum to exactly zero.
- **Status:** Resolved (our fix). The fix is verified offline only; it has not been re-run live.

### FL-014: Anomaly monitor timestamps have nanosecond precision
- **Tool/service:** AWS Cost Anomaly Detection `GetAnomalyMonitors` (`LastEvaluatedDate`)
- **Stage:** Live verification (reported run, 2026-10-16)
- **What we tried:** reading when a monitor was last evaluated, to report freshness and flag stale data.
- **Expected behaviour:** a documented date or timestamp format.
- **Actual behaviour (observed in the reported 2026-10-16 run; raw output not retained):** `LastEvaluatedDate` came back as an ISO timestamp with nine fractional digits (for example `2026-10-16T12:08:00.323472256Z`, as recorded in `docs/cost-intelligence.md`). Before that run we didn't know whether it would be a date or a timestamp.
- **Impact:** Low. A parser written for microseconds or plain dates could reject it.
- **Workaround or resolution (application code):** we use only the date part (`_date` in `backend/cloudguard/cost/anomalies.py` reads the first 10 characters), which accepts a date or a timestamp of any precision. The same helper parses anomaly dates. This is tested offline with fixtures only.
- **Amazon improvement suggestion:** state the exact format and precision of `LastEvaluatedDate` and the anomaly date fields in the API reference.
- **Status:** Resolved (workaround) for `LastEvaluatedDate`.
  - **Live-observed:** `LastEvaluatedDate` only.
  - **Not yet verified:** the anomaly date fields (`AnomalyStartDate`, `AnomalyEndDate`). No anomaly was returned live, so their real format has never been seen.

### FL-015: A live multi-region scan is far slower than a voice turn or an API Gateway integration
- **Tool/service:** AWS Lambda (Function URL or API Gateway), Lambda Web Adapter, read-only discovery APIs across regions
- **Stage:** Deployment preparation
- **What we tried:** planning to serve the governance tools from Lambda for a voice client.
- **What we measured:** a live governance scan took about 101 seconds over 18 enabled regions on 2026-10-20 (`backend/deploy/lambda/README.md`). That was a scan with the developer identity, not the MCP server running live or a Lambda invocation.
- **Constraint:** behind API Gateway, the 29–30 second integration limit would cut a cold scan off; a Function URL allows up to the function timeout. Voice clients will likely time out well before 101 seconds (an estimate: we have no Alexa+ timeout figure).
- **Impact:** Medium. Long-running tools need a design change before a voice deployment.
- **Workaround or resolution:**
  - **Implemented and tested offline (uncommitted working-tree code, fixture mode):** an in-process scan cache. A scan is reused while it is younger than `SCAN_MAX_AGE` (5 minutes), and a forced refresh within 60 seconds is deferred (`backend/cloudguard/server/governance_tools.py`; tests under "scan reuse" and "M2 scan concurrency and refresh" in `backend/tests/test_mcp_governance_tools.py`). It lives in the server process's memory, so on Lambda it would be per execution environment.
  - **Not live-verified, not deployed:** the cache has not been exercised against live AWS through the MCP server, and nothing has been deployed.
  - **Design only, not implemented:** a function timeout of at least 300 seconds, reserved concurrency to limit parallel scans, and options for a warmed cache or an asynchronous "start scan, then fetch results" flow (`backend/deploy/lambda/README.md`).
- **Amazon improvement suggestion:** guidance for voice and MCP clients on handling tools that outlast one turn, with documented client timeouts.
- **Status:** Open (design). The Lambda Web Adapter, the image build and any Lambda invocation are **Not yet verified**: nothing has been built or deployed.
- **Update 2026-10-23:** the scan cache is now committed (`bdbf8cc`) and is keyed by scope (region and service). That means a one-region, one-service question scans only that scope (`backend/tests/test_scoped_inventory.py`). This narrows the latency problem but doesn't remove it for full scans. One cache test fails intermittently (FL-022).

### FL-016: No Alexa+ access to test against, and no OAuth account-linking contract
- **Tool/service:** Alexa+ (MCP integration), account linking
- **Stage:** Development and deployment preparation
- **What we tried:** exposing CloudGuard as a self-hosted MCP server for Alexa+, with account linking.
- **Actual behaviour:**
  - The organisers' FAQ says Alexa+ devices and dev tools are partner-only, and that a web page acting as a real MCP client is an acceptable demo. **Reference:** the Amazon Developer Hackathon FAQ on Devpost, as summarised in `HANDOVER.md` ("Hackathon context") and `docs/product-feedback.md`, and checked against the official rules, overview page and FAQ on 2026-10-13 (project plan notes). The exact FAQ URL was not recorded in this repository and needs to be recovered.
  - We found no documentation, in this repository's research, of what Alexa+ requires for OAuth 2.1 account linking with a self-hosted MCP server: the issuer, audience, scopes and redirect URIs (`backend/deploy/lambda/README.md`, "Blockers before a real deployment").
- **Impact:** High for the Alexa+ track. We can't confirm how Alexa+ would discover tools, use `speech` and structured content, or link accounts.
- **Workaround or resolution:**
  - a browser simulator (`web/`) that is a real MCP client of our server, with Web Speech API voice; it is **not** an Alexa+ integration;
  - an HTTP prototype with static bearer tokens and an `alexa:read` scope; it refuses to start in OAuth mode instead of faking it.
- **Amazon improvement suggestion:** a sandboxed Alexa+ MCP test environment or conformance client for non-partners, and a documented OAuth account-linking contract for self-hosted MCP servers.
- **Status:** Open. **Not yet verified** against Alexa+: no Alexa+ integration exists.
- **Update 2026-10-23:** still open. The Alexa-scoped allow-list now names all fourteen read-only tools (`backend/cloudguard/deploy/http_app.py`, `ALEXA_TOOLS`), and the server refuses to start if any of them is missing, not read-only, or has no projection. The Alexa token cannot reach `/operator`, so the Alexa surface has no write path.

### FL-017: Browser speech recognition mishears AWS terms, and interim text arrives before final text
- **Date:** 2026-10-23
- **Tool/service:** browser Web Speech API (`SpeechRecognition` / `webkitSpeechRecognition`, Chrome/Edge). Not an Amazon tool; logged because it drives the voice-first Alexa+ demo.
- **Area:** voice input (`web/src/lib/voice.ts`, `web/src/components/VoicePanel.tsx`)
- **Stage:** Development
- **Problem:** spoken requests contain service names, resource names and tag keys ("Lambda", "DynamoDB", "CodeBridge", "owner"). The recogniser returns interim guesses before a final result, gives a confidence score only on some results, and has no way for us to bias it toward AWS vocabulary.
- **Root cause (established from the API, not from measurements):** we use `interimResults=true` and `continuous=true` (`voice.ts`), so interim text is a guess that can still change. We don't use a `SpeechGrammarList`, and Chrome doesn't support grammar biasing in practice. **Custom vocabulary is not implemented.**
- **Mitigation / implementation:**
  - recognition language is selectable (en-IN, en-US, en-GB). The default is en-IN when the browser languages or time zone point to India, otherwise en-US or en-GB (`RECOGNITION_LANGS`, `defaultRecognitionLang`). The choice is not persisted;
  - final and interim text are kept apart. Text that was never finalised is flagged `final: false`, and it can't confirm, cancel or answer a pending question (the app replies `PARTIAL_TEXT` and asks again), nor start a tagging request;
  - confidence below `LOW_CONFIDENCE = 0.6` adds "I'm not sure I heard that correctly" to the read-back and shows a warning pill. It warns and doesn't block;
  - every spoken tagging request is **read back before any resource discovery** ("I heard … Is that right?"), and only a spoken yes continues;
  - a "Misheard? Correct the request" form replaces the transcript. The read-back and spoken yes are still required, and it is refused while a confirmation is pending;
  - on the server, voice confirmations accept only exact phrases (`backend/cloudguard/voice_tagging/confirm.py`).
- **Remaining friction:**
  - technical terms are still misheard. The read-back catches the error, but the user has to correct it;
  - an interim-only *read-only question* can still run a read-only tool (only tagging and confirmations are blocked);
  - the error text for "language-not-supported" always says "English (US)", whatever language is selected.
- **Verification evidence:** unit tests in `web/tests/` (733 passed, 2026-10-23) and Playwright `web/e2e/voice.spec.ts`, `voice-tagging.spec.ts` (46 passed in total, 2026-10-23). In both, the browser speech APIs are **faked**. **No real-microphone test is recorded:** accuracy with real speech, accents and noise is **Not yet verified**.
- **Amazon improvement suggestion:** n/a (browser API). For Alexa+, documentation of how MCP tool input is derived from speech (and whether domain vocabulary can be hinted) would let us drop this layer.
- **Priority:** High (it is the main input path of the demo).
- **Status:** Open (mitigated).

### FL-018: Speech output and the microphone fight each other
- **Date:** 2026-10-23
- **Tool/service:** browser `speechSynthesis` and `SpeechRecognition`. Not an Amazon tool.
- **Area:** voice output and microphone lifecycle (`web/src/lib/voice.ts`)
- **Stage:** Development
- **Problem:** if the microphone opens while an answer is still being spoken, the recogniser can hear CloudGuard's own speech. Some browsers never fire the utterance's `end` event, which leaves the client stuck "speaking". The user also reported that the audio volume changed during use.
- **Root cause:** partly established. The overlap and the missing `end` event are handled in code. That the volume change comes from Windows communications ducking or a Bluetooth headset switching to its hands-free profile when the microphone opens is a **hypothesis**, recorded in a code comment, not measured.
- **Mitigation / implementation:**
  - each utterance cancels the previous one, so speech never overlaps. The utterance object is held so it isn't garbage-collected mid-speech;
  - if no `end` event arrives, a polling fallback treats the utterance as ended after at most `6000 ms + 120 ms × characters`;
  - starting to listen stops speaking first. Auto-listen waits 400 ms and re-checks `speechSynthesis.speaking` before opening the microphone;
  - stop, mute, replay and reset all cancel speech;
  - every utterance sets volume, rate and pitch to 1 explicitly. **There is no other volume or ducking handling.**
- **Remaining friction:** whether the volume is now stable on real hardware (Windows ducking settings, Bluetooth headsets) is **Not yet verified**. No physical test is recorded, so this entry doesn't claim the audio problem is fixed.
- **Verification evidence:** unit tests and Playwright `voice.spec.ts` with faked speech APIs (2026-10-23 runs above).
- **Amazon improvement suggestion:** n/a (browser and OS behaviour).
- **Priority:** Medium.
- **Status:** Open (lifecycle mitigated; audio not verified).

### FL-019: Conversational bulk tagging needs a write path that MCP tokens and AWS tag semantics can't give us
- **Date:** 2026-10-23
- **Tool/service:** MCP (bearer tokens), AWS tagging APIs (Lambda, DynamoDB, EC2, S3), STS `GetCallerIdentity` and `AssumeRole`
- **Area:** conversational bulk tagging (`web/src/lib/tagflow.ts`, `voicewrite.ts`, `confirm.ts`, `operator.ts`; `backend/cloudguard/voice_tagging/`, `governance/custom_tagging/`, `governance/remediation/live_writer.py`)
- **Stage:** Development
- **Problem:** "Tag every Lambda function containing CodeBridge in Mumbai with owner=…" should work by voice, but:
  - an MCP bearer token identifies a configured token, not a person, so it can't carry approval or separation of duties (see also FL-011);
  - AWS tag keys are case-sensitive, so `Owner` and `owner` are two different tags on the same resource. Adding one next to the other quietly creates a near-duplicate;
  - the tagging APIs have no conditional write (FL-005), so what the user approved can change before the write.
- **Mitigation / implementation (workflow as built):**

  | # | Step | Where |
  |---|---|---|
  | 1–3 | Understand the request, read it back, wait for a spoken yes | frontend (`voice.ts`); typed chat skips the read-back |
  | 4 | Ask for the region if missing (one region only) | frontend (FL-020) |
  | 5–7 | Search with MCP `search_resources`, report the match count, disclose incomplete discovery | frontend → backend (read-only) |
  | 8–10 | Ask for the tag key and value, only the missing one if one was given, keeping the context | frontend (`voicewrite.ts`, write mode only; the preview flow asks for both) |
  | 11–13 | Preview: matched, to change, already correct, unchanged (with reasons), overwrites (old → new), `KEY_CASE_CONFLICT` explained | backend `custom_tagging/engine.py` (via MCP `preview_tag_changes`, or `POST /operator/voice-tagging/prepare`) |
  | 14 | Spoken "yes", plus "I confirm the overwrite" when values change; a 2-minute challenge bound to the session and a plan hash | frontend parses it, the backend re-checks the exact phrase (`confirm.py`) |
  | 15 | Approve and execute: the server re-resolves from AWS, requires the same plan hash, approves with a `PersonalAccountWaiver`, assumes the tagging role, writes | backend `voice_tagging/service.py`, `remediation/executor.py`, `live_writer.py` |
  | 16–17 | Read every resource back. Report tagged (and verified), already correct, skipped, failed; `FAILED_VERIFICATION` is never counted as tagged | backend result statuses; `VoiceTaggingPanel.tsx` |

  - **Two modes, kept separate:** *Personal Voice Tagging Mode* (`CLOUDGUARD_VOICE_TAGGING_ENABLED=true`, operator sign-in, allow-listed personal account) runs steps 14–17 for one person. Without it, the conversation ends at the preview with CLI hand-off commands: a different person approves the content hash, and someone other than the approver executes (`docs/live-tagging.md`).
  - **Typed chat can't write.** It only reaches `preview_tag_changes`. Only the voice controller calls `/operator/voice-tagging/*`; the server also requires `channel: "voice"` and the `X-CloudGuard-Voice` header (a client convention; the operator session is what authorizes).
  - Writes are outside MCP: no MCP tool writes, and the MCP and Alexa tokens can't reach `/operator`.
  - Case conflicts become `KEY_CASE_CONFLICT`, with no change for that resource, and are spoken to the user.
  - At most 50 resources per confirmation; a kill switch (file or button) stops before the next write.
- **Remaining friction:**
  - **Not yet verified against real AWS:** no live tag write, no real tagging role, no real eventual consistency;
  - the challenges and operator sessions live in server memory (one process; a restart voids them);
  - FL-003, FL-005 and FL-006 still apply to the live writer.
- **Verification evidence:** backend `tests/test_voice_tagging.py`, `test_live_tagging.py`, `test_alexa_tag_preview.py`, `test_lambda_tag_selection.py` (part of the 2026-10-23 backend run, see FL-022); web `tests/confirm.test.ts`, `search-and-tagging.test.ts`, Playwright `tagging.spec.ts`, `voice-tagging.spec.ts` (fixture backend, faked speech).
- **Amazon improvement suggestion:** a conditional tagging API (FL-005), and, for Alexa+, a documented way to bind a confirmed voice action to a verified person.
- **Priority:** High.
- **Status:** Implemented; offline and fixture verified only. Live **Not yet verified**.

### FL-020: Resource discovery needs a region, and a silent default would answer the wrong question
- **Date:** 2026-10-23
- **Tool/service:** EC2 `DescribeRegions`; regional discovery APIs
- **Area:** region clarification (`web/src/lib/regions.ts`, `discovery.ts`, `voice.ts`, `App.tsx`; `backend/cloudguard/governance/discovery/__init__.py`)
- **Stage:** Development
- **Problem:** "List all Lambda functions" has no region. Discovery APIs are regional, a full scan of every enabled region took about 101 seconds (FL-015), and quietly using a default region gives a confident answer for the wrong place.
- **Mitigation / implementation:**
  - a listing (`get_resource_inventory`, `search_resources`, `list_untagged_resources`) without a region asks "Which AWS region should I search?" before any discovery call, and keeps the original request. The preferred-region chip is display-only;
  - region IDs (`ap-south-1`) and city names (about 20 regions, e.g. Mumbai/Bombay → `ap-south-1`) are accepted. An explicit region skips the question. Ambiguous names ("US East", "Europe") and unknown places get a follow-up question;
  - the backend can only narrow its scope: a region that isn't enabled for the account is refused ("region not enabled for this account");
  - "all regions" is offered only when the tool's `tools/list` schema accepts it;
  - "What about Singapore?" re-runs the last listing in the new region, and a stale in-flight answer for the old region is dropped.

  Example: User: "List all Lambda functions." CloudGuard: "Which AWS region should I search?" User: "Mumbai." CloudGuard searches `ap-south-1`.
- **Remaining friction:** **tagging works in one region at a time.** A multi-region tag request has to be repeated per region.
- **Verification evidence:** web `tests/regions.test.ts`, `tests/region-clarification.test.ts`, Playwright `region.spec.ts` (fixture). Backend region narrowing in `tests/test_scoped_inventory.py` and `test_mcp_governance_tools.py`. Live use is **Not yet verified** through the MCP server.
- **Amazon improvement suggestion:** none needed for the API. For voice, documented guidance for Alexa+ on clarifying turns inside an MCP tool flow (elicitation) would help.
- **Priority:** Medium.
- **Status:** Implemented (fixture verified).

### FL-021: Incomplete discovery is disclosed, but doesn't block tagging
- **Date:** 2026-10-23
- **Tool/service:** discovery APIs (per-region and per-service errors, `AccessDenied`, throttling)
- **Area:** discovery coverage (`coverage_complete`, `service_errors`; `web/src/lib/tagflow.ts`, `voicewrite.ts`, `pages/Governance.tsx`, `pages/Overview.tsx`)
- **Stage:** Development
- **Problem:** when a region or service fails to list, "0 matches" and "we couldn't look" sound alike, and a tagging run based on partial discovery could miss resources.
- **Mitigation / implementation:**
  - responses carry `coverage_complete`. A `false` value is spoken and shown as a warning, and service errors are listed;
  - zero matches ("nothing to tag", the flow closes) is reported differently from a search error ("the search didn't work");
  - incomplete results are not cached (`web/src/lib/cache.ts`);
  - a resource whose tags couldn't be read becomes `INSPECTION_FAILED`, never "untagged", and gets no change.
- **Remaining friction:** **incomplete discovery doesn't block or restrict a tagging execution.** The resources that were found can still be tagged after the warning. Whether that is the right policy hasn't been decided.
- **Verification evidence:** web unit tests and Playwright `resources.spec.ts`, `cache.spec.ts` (fixture). Live partial-failure behaviour is **Not yet verified**.
- **Amazon improvement suggestion:** none specific.
- **Priority:** Medium.
- **Status:** Open (disclosed, not enforced).

### FL-022: A scan-cache test fails intermittently in the full backend suite
- **Date:** 2026-10-23
- **Tool/service:** none (our test code)
- **Area:** backend tests (`backend/tests/test_scoped_inventory.py::test_cached_scans_are_never_shared_across_accounts`)
- **Stage:** Testing
- **Problem:** a previous implementation report recorded 2,168 passed, 3 skipped and 1 failed, with this test failing in the full suite but passing on its own.
- **Root cause (hypothesis supported by reading the code; not proven by a reproduction):** the test mixes two clocks.
  - The cache age is `ctx.clock() - scan.completed_at` (`server/governance_tools.py`, `_Scans._age`). The test sets `ctx.clock` to a fake clock frozen at the test's start, then advances it by `MIN_REFRESH_INTERVAL + 1 s` (11 s).
  - But `completed_at` comes from the real `datetime.now(UTC)` (`governance/compliance.py`, `run_scan`).
  - If more than about one real second passes before the second scan completes, the cached scan looks younger than 10 s. The forced refresh is then deferred, and the old account's scan is returned.
  - That makes it timing-dependent: a slower or busier full-suite run fails, a fast isolated run passes. We found no shared global state: each call builds a new server, and there is no autouse fixture.
- **Mitigation:** none yet. Candidate fixes: have `run_scan` take the injected clock, or advance the fake clock further. No application code was changed in this documentation update.
- **Remaining friction / verification evidence:**
  - Re-run on 2026-10-23 at `bdbf8cc`: the full suite (`backend\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`) gave **2,169 passed, 3 skipped, 0 failed** in 637 s. `tests/test_scoped_inventory.py` on its own passed 20/20 three times.
  - So the failure **did not reproduce** in this run. That fits a timing-dependent flake, but doesn't resolve it. The clock mismatch is still in the code, so the test can fail again on a slower run.
- **Priority:** Medium. The cross-account isolation that the test protects is product-critical, even though the failure looks like a test-timing artefact.
- **Status:** **Open** (intermittent; not reproduced on 2026-10-23; root cause hypothesised, not fixed).

### FL-023: Verification categories are easy to blur
- **Date:** 2026-10-23
- **Tool/service:** botocore `Stubber`, `moto`, project fakes, real AWS
- **Area:** testing and claims
- **Stage:** Testing
- **Problem:** the project now has five different kinds of evidence, and a green suite in one doesn't prove the others.
- **Current state:**

  | Category | What it covers | Status (2026-10-23) |
  |---|---|---|
  | Fixture tests | Simulated account (`fixtures/*.json`), fixture executor, fixture MCP backend for Playwright | Run: backend 2,169 passed / 3 skipped (FL-022), web 733 unit + 46 Playwright passed |
  | Simulated speech | Browser speech APIs replaced by fakes in unit and Playwright tests | Run; **no real-microphone or real-speaker test recorded** |
  | Mock AWS | botocore `Stubber`, `moto` (DynamoDB), `FakeAws` for the live writer and voice tagging | Run, inside the backend suite |
  | Read-only live AWS | Discovery (2026-10-20, 18 regions), Cost Explorer CLI (2026-10-16, reported) | Partial. The MCP server, live voice tagging preview and identity tool have **not** been run live |
  | Live AWS writes | Live tag writer, Personal Voice Tagging Mode | **Never performed. Not verified** |
- **Remaining friction:** none of this is production-ready on fixture evidence alone.
- **Priority:** High (it's about what we can honestly claim).
- **Status:** Open until live read and write runs are recorded.

## 4. What worked well
- **botocore `Stubber`:** made almost every AWS code path testable offline, including pagination, throttling, `AccessDenied` and odd responses.
- **`moto` for DynamoDB:** ran our conditional `TransactWriteItems` repositories in tests next to the in-memory and on-disk stores, so concurrency and idempotency tests ran against three backends.
- **DynamoDB conditional transactions:** mapped cleanly onto an append-only, all-or-nothing audit (state head, event, decision, nonce and idempotency record in one write). Conditions on the lease made fencing simple to express.
- **STS `GetCallerIdentity`:** needs no IAM permission, so it's an easy, reliable way to bind every scan and approval to a verified account and principal.
- **boto3 standard retry mode:** gave sensible throttling and 5xx handling with no custom code (`Config(retries={"mode": "standard"})`).
- **`ListBuckets` with `BucketRegion`:** made per-region S3 scans straightforward.
- **Cost Explorer, one request per bill:** one `GetCostAndUsage` call (MONTHLY, `UnblendedCost`, grouped by SERVICE and RECORD_TYPE) covers both the report and the month-over-month comparison.
- **Service Authorization Reference:** the condition-key tables gave us a definitive answer for FL-003, so we could design around it before writing any IAM.
- **Read-only live discovery:** ran against a real account with a developer identity, without surprises beyond FL-001 and FL-002.
- **Cost Explorer live (reported run, 2026-10-16; raw output not retained):**
  - `RECORD_TYPE` grouping returned `Credit`, `Tax` and `Usage` lines that our classification table handled, so credits stayed separate from charges in one request;
  - an exclusive `End` worked as documented for month to date: `Start=2026-10-01`, `End=2026-10-17` (tomorrow in UTC) was accepted and included the current day.

## 5. Workarounds and solutions
| Friction | Workaround in the code or design | Where |
|---|---|---|
| FL-001 | `NoSuchTagSet` → empty tag set | `discovery/s3_discovery.py` |
| FL-002 | Legacy location mapping; ARN de-duplication; unresolved buckets reported once | `discovery/s3_discovery.py` |
| FL-003 | Merge full tag set; whole-set revalidation; exact-ARN session scope | `docs/phase4-iam-design.md`, `remediation/revalidation.py` |
| FL-004 | Exact keys with `StringEqualsIgnoreCase`; application-side reserved prefix | `docs/phase4-iam-design.md`, `custom_tagging/validation.py` |
| FL-005 | Pre-write re-read, fenced write intents, deadlines, post-write verification | `remediation/executor.py` |
| FL-006 | Explicit `identity_note`; drift checks | `remediation/revalidation.py` |
| FL-007 | Explicit RECORD_TYPE table, `unclassified` bucket, reconciliation | `cost/record_types.py`, `cost/bill.py` |
| FL-008 | Documented-not-returned USD, mismatch warning | `cost/models.py`, `cost/anomalies.py` |
| FL-009 | `us-east-1` client, pre-flight checklist, capped pagination | `cost/ce.py`, `docs/cost-intelligence.md` |
| FL-010 | Per-item IAM documentation; planned `LeadingKeys` scoping | `docs/aws-permissions.md` |
| FL-011 | Role-level separation of duties (fails safe) | `remediation/identity.py`, `docs/authorization.md` |
| FL-012 | Adapter pinned by digest read from the ECR Public registry API | `backend/deploy/lambda/Dockerfile` |
| FL-013 | One-cent materiality threshold; "approximately zero" speech tolerance | `cost/models.py`, `cost/voice.py`, `tests/test_cost_materiality.py` |
| FL-014 | Parse only the date part of timestamps | `cost/anomalies.py` |
| FL-015 | In-process 5-minute scan cache (implemented, tested offline); long function timeout and reserved concurrency (design only) | `server/governance_tools.py`, `backend/deploy/lambda/README.md` |
| FL-016 | Browser MCP client as the demo; static bearer tokens; no faked OAuth | `web/`, `backend/deploy/lambda/README.md` |
| FL-017 | Selectable en-IN/en-US/en-GB; final-only actions; low-confidence warning; read-back before discovery; transcript correction | `web/src/lib/voice.ts`, `components/VoicePanel.tsx` |
| FL-018 | Cancel-before-speak, `end` fallback, mic opens only after speech ends | `web/src/lib/voice.ts` |
| FL-019 | Writes kept outside MCP; voice-only operator path with challenge, plan hash, re-resolve, read-back; `KEY_CASE_CONFLICT` | `backend/cloudguard/voice_tagging/`, `remediation/live_writer.py`, `web/src/lib/voicewrite.ts` |
| FL-020 | Ask for the region; city-name mapping; backend scope can only narrow | `web/src/lib/regions.ts`, `discovery.ts`, `governance/discovery/__init__.py` |
| FL-021 | `coverage_complete` warnings; incomplete results not cached | `web/src/lib/tagflow.ts`, `cache.ts` |
| FL-022 | None yet (candidate: inject the clock into `run_scan`) | `backend/tests/test_scoped_inventory.py` |

## 6. Suggestions for Amazon
1. **S3 tagging:** support `aws:TagKeys`/`aws:RequestTag` on `s3:PutBucketTagging`, and offer an additive tag API whose IAM action is separate from delete (FL-003).
2. **Tagging concurrency:** add conditional tagging (`IfMatch`, expected tags, or a tag version) to the Lambda, DynamoDB, EC2 and S3 tagging APIs (FL-005).
3. **Cost Explorer docs:** publish the full `RECORD_TYPE` list with API spellings, return a currency with anomaly `Impact` amounts, and add a single API-prerequisites page (FL-007, FL-008, FL-009).
4. **IAM:** add `StringLikeIgnoreCase`, and state explicitly that `BatchWriteItem` covers delete requests (FL-004, FL-010).
5. **S3 consistency:** return an empty `TagSet` for untagged buckets, and canonical region codes from `GetBucketLocation` (FL-001, FL-002).
6. **Identity:** clearer guidance on per-human identity from STS for separation of duties (FL-011).
7. **Cost Explorer data:** document amount precision and that offsetting lines may not sum to exactly zero; state the format and precision of anomaly timestamps (FL-013, FL-014).
8. **Long-running tools:** documented client timeouts and a recommended pattern for tools that outlast a voice turn (FL-015).
9. **Alexa+:** a sandbox or conformance client for non-partners, and a documented OAuth account-linking contract for self-hosted MCP servers (FL-016).

## 7. Outstanding friction / not yet tested
Nothing here is claimed to be broken; it just hasn't been exercised yet.
- **Cost Explorer and Cost Anomaly Detection live** (the 2026-10-16 run confirmed the read calls, `Credit`/`Tax`/`Usage` and `LastEvaluatedDate`; still open):
  - whether the billing-access setting gates the API;
  - the remaining `RECORD_TYPE` values (discounts, fees, refunds) and `GetDimensionValues`;
  - anomaly date formats, and whether ongoing anomalies are returned (no anomaly was returned);
  - Free-plan request charging;
  - the MCP cost tools in live mode.
- **Bedrock and Strands (postponed):**
  - model access in `ap-south-1`;
  - whether Claude on Bedrock (billed through AWS Marketplace) is usable on a Free-plan account, and whether promotional credits cover it;
  - Strands' default region handling.
- **MCP server:** built since 2026-10-16 (FastMCP, protocol 2025-11-25, stateless Streamable HTTP), but not run against live AWS. Elicitation, planned at that date, is not implemented.
- **CloudFormation and Budgets:** no stack has been deployed; template behaviour (including the TTL attribute) is untested.
- **IAM:**
  - the proposed tagger role and session policies (9 open assumptions in `docs/phase4-iam-design.md`);
  - Access Analyzer results;
  - `dynamodb:LeadingKeys`.
- **Discovery:** EC2 discovery has not been exercised live (no instances existed). The S3 `GetBucketLocation` fallback has not been exercised live either: live runs always returned `BucketRegion` (FL-002; `docs/aws-permissions.md`).
- **moto fidelity:** for transaction cancellation reasons and TTL.
- **STS:** ARN formats for Identity Center users.
- **Lambda Web Adapter and deployment:** no image build, no Lambda invocation, no deployment (FL-015).
- **Alexa+:** no access; nothing tested against it (FL-016).
- **Kiro / Kiro Crew:** Kiro is used for project steering files only, and Kiro Crew is not used; no friction to log so far. Log it here if that changes.
- **Added 2026-10-23:**
  - **Live tagging:** no live AWS write has been performed, through either the CLI or Personal Voice Tagging Mode. The tagging role (`infra/iam/proposed/`) is not deployed, and `sts:AssumeRole` into it has never run (FL-019).
  - **Voice:** real-microphone recognition accuracy and real audio-volume stability (Windows ducking, Bluetooth headsets) (FL-017, FL-018).
  - **Discovery:** live partial-failure behaviour, and whether incomplete discovery should block tagging (FL-021).
  - **Tests:** the intermittent scan-cache test (FL-022).
  - **Bedrock and Strands:** still not used. `strands-agents` is declared in `backend/pyproject.toml`, but no code imports it, and nothing calls Bedrock.

---

## Maintaining this log
Whenever you hit meaningful friction with an Amazon or AWS tool, or find a useful workaround, add or update an entry right away, using the format in §3.
- **Evidence:** cite a file, test or error message.
- **Status:** mark anything that hasn't run against real AWS as **Not yet verified**.
- **Number entries** sequentially (FL-012, FL-013, …), and update §5–§7 if the entry affects them.
