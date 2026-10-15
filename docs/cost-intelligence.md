# Cost intelligence (Task 1)

Two read-only capabilities, backed by AWS Cost Explorer and AWS Cost Anomaly Detection:
- **Last month's bill:** the previous AWS billing month (UTC). It covers charges, discounts, credits and refunds as separate figures, plus the cost per service.
- **Cost anomalies:** the findings AWS Cost Anomaly Detection has published for a recent window.

No LLM is involved. Python makes the API calls, does the arithmetic and writes the spoken summary. Code: `backend/cloudguard/cost/`.

**Status:** implemented and tested offline with botocore Stubber. **Not yet run against real AWS.** There is no CLI or MCP server yet.

## Step 1: AWS pre-flight (you do this; CloudGuard never grants itself permissions)

| # | Check | How | Why |
|---|---|---|---|
| 1 | Attach the read-only policy | Attach [`infra/iam/cost-intelligence-policy.json`](../infra/iam/cost-intelligence-policy.json) to the identity CloudGuard runs as. For local development, that's your CLI user. | The current CLI user has no `ce:*` permissions. |
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
- **Neither source is verified against the API yet.** Each line in `record_types` carries its `match_source`, so the first live run shows which entries matched.

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

## Response contract (for the coming MCP tools)
- **Version:** `schema_version: "1"`. Changes are **additive only**: fields are never renamed or removed, and new fields are optional.
- **Timestamps:** `generated_at` is when CloudGuard built the report. `retrieved_at` is when AWS last answered successfully, or null if it never did. Once caching exists, `retrieved_at` is the data's "last updated" time.
- **Cache:** `cache` is reserved (`hit`, `stored_at`, `ttl_seconds`) and is always null until caching is built.
- **Warnings:** `warnings` is a list of codes: `ESTIMATED`, `UNCLASSIFIED_RECORD_TYPES`, `CURRENCY_MISMATCH`, `STALE_EVALUATION`.
- **Never included:** account IDs, ARNs, credentials and raw AWS error messages. Errors carry only the code, the operation, AWS's documented description and the inferred causes.

## Unresolved assumptions (to verify with pre-flight step 4)
1. **RECORD_TYPE strings:** the exact API values are unconfirmed. Discount names are candidates only.
2. **Anomaly currency:** that anomaly amounts are USD rests on AWS's "dollar value" wording, not on a returned field.
3. **Ongoing anomalies:** whether `GetAnomalies` with a `DateInterval` returns anomalies that haven't ended yet is unconfirmed.
4. **Date formats:** the format of `LastEvaluatedDate` and the anomaly dates is unconfirmed. Parsing accepts `YYYY-MM-DD` or an ISO timestamp.
5. **Free plan:** whether the Free plan allows or charges for CE API requests is unconfirmed.
6. **Billing access:** whether the IAM billing-access setting gates the CE API is unconfirmed.
