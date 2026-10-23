# Product Feedback

Feedback from building CloudGuard AI for the Amazon Developer Hackathon (Alexa+ track, AWS Builder mini challenge). It is based only on what we implemented and tested. The evidence for each AWS issue is in [FRICTION_LOG.md](FRICTION_LOG.md).

## What we built
CloudGuard AI is a read-only AWS governance assistant exposed as a self-hosted **MCP server** (Python `mcp` SDK / FastMCP, protocol 2025-11-25, Streamable HTTP). It provides twelve read-only tools.
- **Cost (6):** monthly bill, current month spending, cost breakdown, month comparison, cost-increase attribution, cost anomalies. All use Cost Explorer and Cost Anomaly Detection.
- **Governance (5):** resource inventory, tag compliance scan, untagged resources, finding explanations, evidence-backed tag recommendations.
- **Identity (1):** which AWS account and identity type the server's credentials belong to (STS `GetCallerIdentity`).
- **Determinism:** deterministic code decides every finding and figure, and every result carries raw evidence. Each tool also returns a short `speech` string written for voice. Bedrock and Strands agents were planned but not implemented.

**An Alexa-facing surface** (HTTP deployment prototype, not deployed):
- a separate bearer token with an `alexa:read` scope;
- an explicit twelve-tool allowlist, enforced at `tools/list` and again at `tools/call`;
- an Alexa response projection that removes account IDs, ARNs, resource names, raw tag values and operator evidence from both the structured and the text output. The one deliberate exception is the identity tool, whose answer carries the account ID (never the ARN, IAM name or session name).

**An Alexa-compatible MCP simulator** (`web/`): a React + TypeScript web app that is a real MCP client, using the official MCP TypeScript SDK.
- It does the actual handshake (`initialize`, `notifications/initialized`, `tools/list`) and calls tools with `tools/call`, using the Alexa-scoped token.
- It shows the voice response and the structured evidence separately.
- Voice input and output use the browser's Web Speech API (`SpeechRecognition` and `speechSynthesis`, Chrome and Edge), with a spoken yes/no confirmation before each tool call.
- It is **not** an Alexa+ integration. There was no Amazon account linking, no Alexa device and no Alexa service.

**Verification:**
- All twelve tools were verified in fixture mode (synthetic data, zero AWS API calls, checked with a tripwire), including through the browser.
- Playwright tests run against the real fixture backend. The browser speech APIs are replaced by test fakes, so recognition and synthesis in a real browser are not covered by automated tests.
- Live AWS discovery was exercised only partially. A live run of the cost CLI against Cost Explorer was reported on 2026-10-16, but its raw output was not retained, and the MCP server was never run against live AWS.
- Nothing is deployed.

## The MCP and Alexa+ experience
**What worked well**
- **Building the server was quick.** The Python SDK's FastMCP gives typed input and output schemas from Pydantic models, and its tool annotations (`readOnlyHint`, `destructiveHint`) let a client see from the tool list that every tool is read-only.
- **Streamable HTTP in stateless JSON mode was simple to host:** one `POST /mcp` endpoint, no session state. It fits a Lambda Web Adapter design.
- **The TypeScript SDK client handled the protocol itself:** version negotiation and the `MCP-Protocol-Version` header. The browser client needed very little protocol code.
- **The MCP SDK's DNS-rebinding protection** (allowed hosts and origins) gave a useful default security layer.

**What was difficult**
- **No access to Alexa+ for testing.** Alexa+ MCP integration is partner-only, so we could not test against real Alexa+. We followed the organisers' guidance that a web page acting as a real MCP client is an acceptable demo. As a result, we can't confirm how Alexa+ would discover tools, render `speech`, handle structured content, or link accounts.
- **Account linking (OAuth 2.1) couldn't be built.** It needs an authorization server whose issuer, token-validation keys and audience we did not have. Our prototype refuses to start in OAuth mode rather than fake it, and uses static bearer tokens instead.
- **The output schema is per tool, not per caller.** Giving the Alexa caller a smaller, projected response than an operator meant advertising different output schemas to different callers. The Python SDK caches tool definitions once for all callers and validates outputs against that cache, so we had to return results in a form that bypasses the shared cache.
- **The spec no longer supports JSON-RPC batching,** but older clients may still send it. The SDK refuses batches with HTTP 400. We test that a batch can't get around the allowlist.
- **Browser clients need CORS or a same-origin proxy.** Our server sends no CORS headers, so the simulator uses a same-origin development proxy. A hosted browser client would need an explicit CORS or gateway design.
- **Voice latency.** A cold governance scan across all enabled regions took about 101 seconds. That is far longer than an interactive voice reply allows, so long-running tools need an asynchronous pattern (start a job, then fetch the result) or a warm cache.

**What would help a real Alexa+ integration**
1. A public, sandboxed Alexa+ MCP test environment, or a conformance test client, so developers outside the partner program can check tool discovery, speech handling and errors.
2. Documented OAuth 2.1 account-linking requirements for self-hosted MCP servers: issuer, audience, scopes, token lifetime and how the user's identity reaches the server.
3. Guidance on how Alexa+ uses `structuredContent` versus text content, and on the expected length and format of spoken answers.
4. Documented client timeouts, plus a recommended pattern for tools that take longer than a voice turn.
5. A way for one server to advertise a client-specific tool set and output schema, for example per scope, without workarounds.

## AWS services
| Service | How CloudGuard uses it | Status |
|---|---|---|
| AWS Cost Explorer and Cost Anomaly Detection | Bill, breakdown, comparison and anomaly tools (read-only) | Fixture-verified. One reported live Cost Explorer CLI run occurred on 2026-10-16, using `GetCostAndUsage`, `GetAnomalyMonitors` and `GetAnomalies`; raw output was not retained, and `GetDimensionValues` was not run. The MCP server itself has not been tested against live AWS. Free-plan request charges and credits have not been verified. |
| Lambda, DynamoDB, S3, EC2, STS (read APIs) | Resource discovery and tag compliance | Partly verified live (Lambda, DynamoDB, S3 read once); EC2 not exercised |
| AWS Lambda Web Adapter | HTTP deployment prototype | Built and tested locally; not deployed |
| CloudFormation, IAM, DynamoDB (governance table), Budgets | Templates and policies for the inspector role, governance store and budget | Written; not deployed |
| Amazon Bedrock, Strands Agents SDK | Planned for explanations | Not implemented |
| Kiro | Steering files in `.kiro/steering/` | Used for project steering only |

**What worked well**
- The boto3 read APIs and botocore's `Stubber` made deterministic offline tests easy.
- Cost Explorer's `RECORD_TYPE` grouping separates charges, credits and refunds in a single request.

**What needs work** (details and evidence in the friction log)
- **S3 tagging.** S3 reports an untagged bucket as an error (`NoSuchTagSet`, FL-001). `PutBucketTagging` replaces the whole tag set and can't be limited by tag key in IAM (FL-003).
- **No conditional tag writes.** The tagging APIs have no conditional write or fencing token, so there is always a small gap between a check and a write (FL-005).
- **Cost Explorer documentation.** `RECORD_TYPE` values aren't listed in the API reference (FL-007). Anomaly amounts have no currency field (FL-008). Prerequisites and permissions are hard to confirm up front (FL-009).
- **IAM semantics.** DynamoDB transaction and batch IAM behaviour is easy to misread (FL-010). STS assumed-role identity can't tell apart people who share a role (FL-011).
- **Lambda Web Adapter** documentation gives an image tag but no digest to pin (FL-012).

## Feature requests
| Request | Why it matters | Priority |
|---|---|---|
| Alexa+ MCP sandbox or conformance client for non-partners | We couldn't test against real Alexa+ | Critical |
| Documented Alexa+ OAuth account-linking contract for self-hosted MCP servers | Needed to replace static tokens | Critical |
| Conditional (compare-and-set) tag writes | Safe approved tagging without race windows | Important |
| A published, versioned list of Cost Explorer `RECORD_TYPE` values | Correct cost totals without guesswork | Important |
| Per-scope tool sets and output schemas in the MCP SDKs | A clean way to minimise data for voice clients | Nice-to-have |
