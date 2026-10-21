# Cost intelligence (Task 1)

Two read-only capabilities, backed by AWS Cost Explorer and AWS Cost Anomaly Detection:
- **Last month's bill:** the previous AWS billing month (UTC). It covers charges, discounts, credits and refunds as separate figures, plus the cost per service.
- **Cost anomalies:** the findings AWS Cost Anomaly Detection has published for a recent window.

No LLM is involved. Python makes the API calls, does the arithmetic and writes the spoken summary. Code: `backend/cloudguard/cost/`.

**Status:** implemented and tested offline with botocore Stubber and synthetic fixtures. A read-only CLI (see "Cost Intelligence CLI") and a local MCP server with six read-only cost tools (see "MCP server") exist. **Live AWS:** a live run of the cost CLI against Cost Explorer was reported on 2026-10-16 (the observations are under "Unresolved assumptions"); its raw output was not retained. The MCP server has **not** been run against real AWS. Not deployed, and not connected to Alexa+.

## Step 1: AWS pre-flight (you do this; CloudGuard never grants itself permissions)

| # | Check | How | Why |
|---|---|---|---|
| 1 | Attach the read-only policy | Attach [`infra/iam/cost-intelligence-policy.json`](../infra/iam/cost-intelligence-policy.json) to the identity CloudGuard runs as. For local development, that's your CLI user. | Required for live mode. In the reported live run on 2026-10-16 (raw output not retained), the developer CLI identity's `GetCostAndUsage`, `GetAnomalyMonitors` and `GetAnomalies` calls succeeded. |
| 2 | Cost Explorer is enabled | Open Billing and Cost Management, then Cost Explorer, once. | AWS documents a delay of up to about 24 hours before data is ready after first enablement. |
| 3 | Billing access for IAM identities | As root: Account settings, then "IAM user and role access to Billing information", then Activate. | This gates the console for IAM identities. Whether it also gates the CE **API** isn't confirmed; step 4 tells us. |
| 4 | Smoke test (4 requests, about $0.04) | Run the commands below and keep the output. | It proves permissions and data availability, and gives us the **real RECORD_TYPE values** (see "Unresolved assumptions"). |
| 5 | An anomaly monitor exists | `get-anomaly-monitors` (below) | Without a monitor, "no anomalies" means nothing. |
| 6 | Free-plan charging for CE API calls | Billing console, Free plan page, and Cost Explorer pricing | A request may be charged against credits or refused. |

```bash
aws ce get-dimension-values --region us-east-1 --time-period Start=2026-08-01,End=2026-10-01 --dimension RECORD_TYPE
```
```bash
aws ce get-cost-and-usage --region us-east-1 --time-period Start=2026-09-01,End=2026-10-01 --granularity MONTHLY --metrics UnblendedCost --group-by Type=DIMENSION,Key=RECORD_TYPE
```
```bash
aws ce get-anomaly-monitors --region us-east-1
```
```bash
aws ce get-anomalies --region us-east-1 --date-interval StartDate=2026-10-01
```

The Cost Explorer API has one endpoint, `us-east-1`. Use it even though the account's resources are in `ap-south-1`.

## IAM permissions

| API call | IAM action | Used for |
|---|---|---|
| `ce.get_cost_and_usage` | `ce:GetCostAndUsage` | Last month's bill |
| `ce.get_anomaly_monitors` | `ce:GetAnomalyMonitors` | Tell "no monitor" apart from "no anomalies"; latest evaluation date |
| `ce.get_anomalies` | `ce:GetAnomalies` | Anomaly findings |

`ce:GetDimensionValues` is **not** in the runtime policy. It is used only once, by you, in pre-flight step 4, so that identity needs `ce:GetDimensionValues` temporarily (or run it as an identity that already has it).

## Billing period: AWS billing month in UTC
- **UTC is canonical.** AWS documents that Cost Explorer days and months are UTC. Task 1 reports the previous **AWS billing month**: the UTC calendar month before the one the current UTC instant falls in.
- **Dates sent to AWS:** date-only strings, with End exclusive. September 2026 is `Start=2026-09-01, End=2026-10-01`.
- **Response labels:** every bill response carries `billing_timezone: "UTC"` and `period_basis`.
- **Callers' clocks:** naive datetimes are refused. An aware datetime in any timezone is converted to UTC. So at 03:00 IST on 1 October (still 30 September in UTC), "last month" is August.
- **Local calendars:** local-calendar reporting is a possible later feature. It would need DAILY or HOURLY data, and even then AWS's days are UTC days.
- **No new dependency:** this uses only Python's built-in `datetime.UTC`, so **no `tzdata` dependency is needed**.

## Last month's bill

**One request** to `GetCostAndUsage`:
- `Granularity=MONTHLY`
- `Metrics=[UnblendedCost]`
- `GroupBy=[SERVICE, RECORD_TYPE]`
- The period runs from the start of the month before last up to the start of the current month (exclusive), so the comparison costs nothing extra.

**Metric:** only `UnblendedCost` is used. It's stated as `metric` in every report.

### RECORD_TYPE classification (`cost/record_types.py`)
Every returned line is placed in **exactly one** category. Matching ignores case, spaces, hyphens and underscores. **Unknown values are `unclassified`.** They are never counted as charges or in a service total; they're listed by name and raise the `UNCLASSIFIED_RECORD_TYPES` warning.

| Category | Values matched | Source | Accounting treatment |
|---|---|---|---|
| `usage` | Usage, Reservation applied usage, Savings Plan covered usage | Console doc | Charge. Savings Plan covered usage appears at **on-demand rates** under UnblendedCost. |
| | DiscountedUsage | Candidate | |
| `savings_plan_negation` | Savings Plan negation | Console doc | Charge (negative): offsets the covered usage |
| `fee` | Recurring reservation fee, Upfront reservation fee, Savings Plan recurring fee, Savings Plan upfront fee, Support fee, Other out-of-cycle charges | Console doc | Charge |
| `tax` | Tax | Console doc | Charge, also reported on its own |
| `discount` | Bundled discount, Enterprise discount program discount, EdpDiscount, Private rate discount, Distributor discount, Solution provider program discount, SppDiscount | **Candidate** | Reported separately; not in service totals |
| `credit` | Credit | Console doc | Reported separately; not in service totals |
| `refund` | Refund | Console doc | Reported separately; not in service totals |
| `unclassified` | anything else | — | Reported separately; never a charge |

- **Console doc:** named in the [Cost Explorer user guide's Charge type list](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-filtering.html).
- **Candidate:** a plausible API spelling or billing line-item type that isn't in that list.
- **Partly verified against the API.** In the reported live run on 2026-10-16 (raw output not retained), `GetCostAndUsage` grouped by `RECORD_TYPE` returned `Credit`, `Tax` and `Usage`. Every other value, including all discount spellings, is still unverified, and `GetDimensionValues` was never run. Each line in `record_types` carries its `match_source`, so a live run shows which entries matched.

**Figures and identities.** The tests check all of these, including on 200 randomised lines.

```
charges_before_discounts_credits_and_refunds = usage + savings_plan_negation + fees + tax
calculated_net = charges_before_discounts_credits_and_refunds + discounts + credits + refunds + unclassified
               = sum(record_types.amount)       # every returned amount, exactly once
sum(services.amount) = charges_before_discounts_credits_and_refunds
```

- **`calculated_net`** is labelled by `net_basis` as a **CloudGuard calculation, not the AWS invoice amount**. The spoken reply says so too.
- **Comparison:** month-over-month change in `charges_before_discounts_credits_and_refunds`.
- **Estimated data:** an `Estimated: true` month (normal until AWS finalises the bill) raises the `ESTIMATED` warning.

### Bill statuses
| Status | Meaning |
|---|---|
| `OK` | Data returned |
| `NO_DATA` | The request succeeded, but there are no cost lines for the month. We never say "$0". |
| `DATA_UNAVAILABLE` | AWS returned `DataUnavailableException`. AWS's documented meaning, "The requested data is unavailable.", goes in `error.aws_description`. CloudGuard's guesses (not enabled yet, still preparing, outside retention) go separately in `error.inferred_possible_causes`. We never claim the cause. |
| `ACCESS_DENIED` | AccessDenied-type error. Inferred causes are listed separately. |
| `MIXED_CURRENCY` | Amounts came in more than one unit. They are never summed. |
| `ERROR` | Any other failure, including `PageLimitExceeded` |

## Cost anomalies
1. **`GetAnomalyMonitors`.** If there are no monitors, the status is `NO_MONITOR`. If no monitor has a `LastEvaluatedDate`, it's `NOT_YET_EVALUATED`. Either way, `GetAnomalies` is not called.
2. **`GetAnomalies`** over `[today − lookback, today]` (UTC). The default look-back is 7 days, the maximum 90.

**Freshness:**
- The latest monitor evaluation date is reported as `last_evaluated`.
- If it's more than 2 days old, `stale=true` and a `STALE_EVALUATION` warning are added.
- Detection is never described as real-time.

**`NO_ANOMALIES` caveat:** the reply always says this doesn't guarantee spending is normal.

**Expected spend of zero:** AWS documents that it omits `TotalImpactPercentage` when expected spend is zero. The explanation says so.

### Anomaly currency
- **What AWS returns:** the [Impact](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_Impact.html) data type describes its fields as dollar values ("The dollar value of the anomaly"), but the response has **no currency field**.
- **What we report:** `currency: "USD"`, plus `currency_basis: "documented_not_returned: …"` and `currency_reference` (the doc URL).
- **Bill currency differs:** if the caller passes a bill currency other than USD (`bill_currency=`), the report gets a `CURRENCY_MISMATCH` warning. Nothing is converted.

## Pagination and request cost
- **Page cap:** pagination follows `NextPageToken`, up to `max_pages`, then fails with `PageLimitExceeded` rather than returning a partial result.
- **Configuring the cap:** `max_pages` comes from the function argument, else `CLOUDGUARD_CE_MAX_PAGES`, else 5. The allowed range is 1–20.
- **Request count:** every report carries `aws_requests`, the Cost Explorer requests CloudGuard sent. botocore's own retries (up to 3 attempts) aren't counted, but may also be billed.
- **Price:** AWS lists the Cost Explorer API at about **$0.01 per paginated request**.

| Feature | Typical requests | Worst case at default cap |
|---|---|---|
| Bill | 1 | 5 |
| Anomalies (monitor exists) | 2 | 10 |
| Anomalies (no or unevaluated monitor) | 1 | 5 |

## Response contract (cost reports, CLI and MCP cost tools)
The CLI and the six MCP cost tools (see "MCP server") return these reports. The MCP cost tools are implemented and tested offline with fixtures and botocore Stubber. They have **not** been run against live AWS, the server is not deployed, and there is no Alexa+ integration.

- **Version:** `schema_version: "1"`. Changes are **additive only**: fields are never renamed or removed, and new fields are optional.
- **Timestamps:** `generated_at` is when CloudGuard built the report. `retrieved_at` is when AWS last answered successfully, or null if it never did. Once caching exists, `retrieved_at` is the data's "last updated" time.
- **Cache:** `cache` is reserved (`hit`, `stored_at`, `ttl_seconds`) and is always null until caching is built.
- **Warnings:** `warnings` is a list of codes: `ESTIMATED`, `UNCLASSIFIED_RECORD_TYPES`, `CURRENCY_MISMATCH`, `STALE_EVALUATION`, `MONTH_TO_DATE`.
- **Never included:** account IDs, ARNs, credentials and raw AWS error messages. Errors carry only the code, the operation, AWS's documented description and the inferred causes.

## Cost Intelligence CLI

`python -m cloudguard.cost` (run from `backend/`, for example `.venv\Scripts\python.exe -m cloudguard.cost bill`). It's read-only and tests the cost answers before they're exposed over MCP. Code: `cost/__main__.py`, `cost/compare.py`, `cost/fixture.py`, `cost/summary.py`, plus speech in `cost/voice.py`.

| Question | Command |
|---|---|
| What was my AWS bill last month? | `bill` (default `--period last-month`) |
| How much have I spent this month? | `bill --period this-month` |
| Compare September and October | `compare --months september october` (default: the last two completed months) |
| Which AWS service costs me the most? | `top-services [--period ... \| --month ...]` |
| Why did my AWS costs increase? | `explain-increase [--months EARLIER LATER]` |
| Are there any unusual charges? | `anomalies [--lookback-days N]` |

**Common options:**
- `--mode fixture|live` (default `fixture`).
- `--as-of YYYY-MM-DD` (UTC): the reference date. The default is today in live mode, and the fixture's `as_of` in fixture mode.
- `--format summary|json|speech` (default `summary`).
- `--fixture NAME|PATH` (fixture mode only).
- `--profile` (live mode only).
- `--max-pages N`.

**Month arguments:** a month is `YYYY-MM`, a month name or an abbreviation of three letters or more (its latest occurrence not after `--as-of`), `last-month`, or `this-month`.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Answered. This includes "no anomalies" and "the cause can't be determined". |
| 1 | AWS Cost Anomaly Detection reported anomalies. |
| 2 | Invalid arguments, for example a bad or future month, or an unknown fixture. |
| 3 | AWS/API error or data unavailable: access denied, throttling, `DataUnavailableException`, a page cap hit, no cost data, mixed currencies, no monitor, a monitor never evaluated, or a profile/credentials problem. |
| 4 | Internal error, including a malformed or non-synthetic fixture. |

**Output rules:**
- **The current month is always month to date.** Its period `kind` is `month_to_date`. The summary heading says "NOT a final bill", and the spoken reply says "month-to-date spending, not a final bill".
- **Completed months:** `kind` is `completed_month`. AWS may still mark them `ESTIMATED`.
- **Month-to-date request range:** `Start=` the first of the month and `End=` the day after `--as-of`, so the as-of day is included. Cost Explorer accepted this in live verification on 2026-10-16 (`Start=2026-10-01`, `End=2026-10-17`).
- **Comparison with a month in progress:** `compare` and `explain-increase` use `basis: matching_days`. The earlier month is cut to the same number of days with a second request, and its full-month total is reported only as context. A partial month is never compared with a full one.
- **Top services:** ranked by charges before discounts, credits and refunds. AWS's tax-only `Tax` line is excluded from the ranking and the spoken reply, but disclosed as `ranking.excluded_tax` and in the summary.
- **Increase attribution is arithmetic, not diagnosis.** The calculated net change splits exactly into per-service charge changes, plus discount, credit, refund and unclassified changes; the tests check this.
  - **Materiality threshold:** `MATERIALITY_THRESHOLD = 0.01` in the report currency's units (`models.py`), one cent, the smallest amount an invoice shows. Live Cost Explorer amounts carry about 10 decimal places, and when credits cover usage the lines leave residues such as `1.5E-9`. Those are noise, not spend.
  - **Which contributions count:** only contributions of at least the threshold are listed as drivers or offsets. The rest are totalled in `below_threshold_change`. Drivers, offsets and that total always add up exactly to `net_change`.
  - **Comparisons are exact:** they use Decimal throughout, and billing figures are never rounded or suppressed.
  - **`INCREASE_ATTRIBUTED`:** the net rose by at least the threshold. It names where the increase appears, never why.
  - **`NO_INCREASE`:** the net fell by at least the threshold.
  - **`NO_MATERIAL_CHANGE`:** the net moved by less than the threshold, including exactly zero.
  - **`CANNOT_DETERMINE`:** the net rose materially, but either the largest contribution is unclassified, or no single contribution reaches the threshold.
  - **Spoken reply:** always says CloudGuard can't see why usage changed.
- **Spoken nets** take their sign from the unrounded value:
  - "a net charge of 0.25";
  - "a net credit of 0.50" (never "minus");
  - "a net of zero";
  - "a net of approximately zero (charges and credits approximately balance)" when the magnitude is below `SPEECH_NET_TOLERANCE = 0.005` (`voice.py`). Such an amount would otherwise be read out as "0.00".

  Spoken month-over-month differences below that tolerance are "about the same". The summary report never prints "-0.00", and shows the exact value next to a near-zero net. JSON keeps exact amounts.
- **JSON envelope:** `schema_version`, `command`, `mode`, `as_of`, `fixture`, `exit_code`, `speech`, then:
  - `period` and `report` (a `BillReport`) for `bill` and `top-services`, plus `ranking` for `top-services`;
  - `report` (a `CostComparison`) for `compare` and `explain-increase`;
  - `report` (an `AnomalyReport`) for `anomalies`.

**Fixture mode** (`backend/fixtures/cost/*.json`, all synthetic):
- **How it runs:** `FixtureCostExplorer` answers the three operations in AWS's response shapes, pages with `NextPageToken`, and raises botocore `ClientError`s. Fixture runs therefore use the same pagination and error handling as live runs.
- **No boto3, credentials or network:** fixture mode imports botocore exception classes, but never boto3, never reads credentials and never opens a network connection. A test checks this in a fresh interpreter.
- **Scenarios:**

| Fixture | Scenario |
|---|---|
| `demo` (default) | Normal completed month with tax, a support fee, a credit and a refund; month-over-month increase; month to date; one ongoing anomaly |
| `decrease` | Costs fell; no anomalies |
| `credits-refunds` | Credits and a refund make the net a credit |
| `savings-plans` | Savings Plan covered usage, negation and recurring fee |
| `unclassified` | Unknown record types drive the increase, so the cause can't be determined |
| `no-monitor` | No anomaly monitor |
| `stale-anomalies` | Monitor last evaluated six days earlier |
| `anomalies-not-evaluated` | Monitor never evaluated |
| `paginated` | 3 groups per page, 1 anomaly item per page |
| `pagination-error` | Throttling on page 2; no partial totals |
| `access-denied` | Access denied |
| `data-unavailable` | `DataUnavailableException` |
| `near-zero-net` | Credits cover usage to within billionths, so nets and month changes are noise (regression for live verification) |

**Live mode:**
- **Calls:** read-only `GetCostAndUsage`, `GetAnomalyMonitors` and `GetAnomalies`, through `ce.make_client`, to the `us-east-1` endpoint.
- **Request counts:** `bill`, `top-services` and full-month `compare` take one paginated request. Matching-days comparisons take two. `anomalies` takes one or two.
- **Live verification:** a live CLI run was reported on 2026-10-16. It is the source of the observations marked "live" under "Unresolved assumptions" (month-to-date `End`, `RECORD_TYPE` values `Credit`/`Tax`/`Usage`, the `LastEvaluatedDate` format). Its raw output was not retained, and the other live-mode paths remain unverified.

## MCP server (cost tools)

`python -m cloudguard.server [--transport stdio|streamable-http] [--port 8000] [--mode fixture|live] [--fixture NAME] [--profile NAME] [--max-pages N]` (run from `backend/`).

**Code:** `cloudguard/server/`. It's the first and only MCP server.
- `app.py` builds it and is the single place tools are registered.
- `cost_tools.py` holds the tools.
- `context.py` holds the startup AWS context.

The CLI and the server both call `cloudguard/cost/service.py`, so the billing logic exists once.

**Server setup:**
- **Framework:** FastMCP from the `mcp` Python SDK 1.30.0 (the version locked in `backend/uv.lock`), protocol 2025-11-25.
- **HTTP transport:** stateless Streamable HTTP with JSON responses on `/mcp`, bound to `127.0.0.1` only. stdio is also available.
- **Default mode:** fixture (synthetic data, no AWS).

| Tool | Question | Inputs |
|---|---|---|
| `get_monthly_aws_bill` | What was my AWS bill last month? | `month` (default `last-month`); the current month is refused |
| `get_current_month_spending` | How much have I spent this month? | none |
| `get_cost_breakdown` | Which AWS service costs me the most? | `month` (default `last-month`), `limit` 1–50 (default 10) |
| `compare_monthly_costs` | Compare September and October | `earlier_month`, `later_month` (both or neither) |
| `investigate_cost_increase` | Why did my AWS costs increase? | `earlier_month`, `later_month` (both or neither) |
| `get_cost_anomalies` | Are there any unusual charges? | `lookback_days` 1–90 (default 7) |

**Inputs:**
- `month` matches `^(\d{4}-\d{2}|last-month|this-month|[A-Za-z]{3,9})$`, is at most 10 characters, and is resolved against the server's as-of date: the fixture's `as_of`, or today (UTC) in live mode.
- Every input schema sets `additionalProperties: false`, and the server rejects unknown arguments; FastMCP would otherwise ignore them silently.
- Invalid input returns a tool error (`isError`).

**Output:** every result is structured content that matches the tool's `outputSchema`.
- **Envelope fields:** `schema_version`, `tool`, `outcome` (`answered` / `findings` / `unavailable`), `mode`, `data_source`, `as_of`, `currency`, `freshness` (`generated_at`, `retrieved_at`, `aws_requests`, `estimated`, `anomaly_last_evaluated`, `anomaly_data_stale`), `speech`, and `report`.
- **Bill tools:** `get_monthly_aws_bill`, `get_current_month_spending` and `get_cost_breakdown` also return `period` and `is_final_bill` (always `false` for month to date).
- **Ranking:** `get_cost_breakdown` also returns `ranking`.
- **`unavailable`:** means no figures were produced. It is never zero spend.

**Annotations:** `readOnlyHint=true`, `destructiveHint=false` and `idempotentHint=true`; `openWorldHint` is true only in live mode.

**AWS account context and authorization:**
- **Credentials:** the server uses its own process credentials (the default chain, or `--profile` at startup) through `ce.make_client`.
- **No caller-chosen context:** no tool accepts an account, profile, role, region or credentials, so an MCP client can't redirect the server.
- **No authentication in the local server (`python -m cloudguard.server`):** whoever can reach it acts with its credentials. That's why its HTTP transport binds to localhost only.
- **The HTTP deployment prototype (`python -m cloudguard.deploy`)** puts the same six tools behind a static bearer token (only its SHA-256 digest is configured, in `CLOUDGUARD_TOKEN_SHA256`), a `cost:read` scope, a host allow-list and a 64 KiB body limit. See `backend/deploy/lambda/README.md`. It is tested offline and on a local loopback server only. It is not deployed, and there is no OAuth.
- **Not wired into the allow-list:** the Phase 4 allow-list (`governance/remediation/authz.py`) has no read permission, so cost tools don't use it yet. Server-side identity verification is still future work (`identity.py`, Phase 7).

**Limitations:**
- No MCP authentication in the local server (`python -m cloudguard.server`): use stdio, or its localhost-only HTTP transport, on a machine you control. The deployment prototype uses a static bearer token, not OAuth.
- Fixture mode is the default and is what the tests cover. Live mode has not been run against real AWS through the MCP server.
- Not deployed, and not connected to Alexa+.

## Live verification and unresolved assumptions
**Verified by the reported live CLI run on 2026-10-16** (developer CLI identity; raw output not retained):
- the `us-east-1` Cost Explorer endpoint worked;
- `GetCostAndUsage`, `GetAnomalyMonitors` and `GetAnomalies` succeeded;
- a month-to-date request was accepted (item 7);
- `RECORD_TYPE` returned `Credit`, `Tax` and `Usage` (item 1).

**Still unresolved** (pre-flight steps 2–4 and 6 remain relevant): the items below, plus how the first-enablement delay appears in the API, and the MCP cost tools in live mode (never run live).

1. **RECORD_TYPE strings:** `Credit`, `Tax` and `Usage` were confirmed live on 2026-10-16. The other values are unconfirmed, discount names are candidates only, and `GetDimensionValues` was never run.
2. **Anomaly currency:** that anomaly amounts are USD rests on AWS's "dollar value" wording, not on a returned field.
3. **Ongoing anomalies:** whether `GetAnomalies` with a `DateInterval` returns anomalies that haven't ended yet is unconfirmed.
4. **Date formats:** `LastEvaluatedDate` was returned live as an ISO timestamp with nanoseconds (for example `2026-10-16T12:08:00.323472256Z`), which parsing handles. Anomaly date formats are unconfirmed, because no anomaly was returned. Parsing accepts `YYYY-MM-DD` or an ISO timestamp.
5. **Free plan:** whether the Free plan allows or charges for CE API requests, and whether credits cover them, is unconfirmed. The 2026-10-16 calls succeeded, but no record of their charge was kept.
6. **Billing access:** whether the IAM billing-access setting gates the CE API is unconfirmed.
7. **Month-to-date End:** verified live on 2026-10-16: `GetCostAndUsage` accepted `End` = tomorrow (UTC).
