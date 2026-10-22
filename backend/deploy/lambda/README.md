# CloudGuard MCP on AWS Lambda: deployment prototype

**Status:** prototype. **Not deployed.** Nothing here creates AWS resources.

The prototype runs the existing CloudGuard MCP server (all twelve read-only tools: six cost, five governance and one identity) as a Streamable HTTP app behind [AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter). A Lambda Function URL request reaches the adapter, which forwards it to uvicorn on `127.0.0.1:$PORT`. From there it goes to `cloudguard.deploy.http_app` and then to the MCP server.

## What has and hasn't been verified

| Level | Status |
|---|---|
| Offline tests (ASGI test client, fixtures, mocked botocore) | Done: `tests/test_http_deploy.py`, `tests/test_deploy_scopes.py`, `tests/test_lambda_prototype.py`, `tests/test_alexa_surface.py` |
| Local HTTP server (uvicorn on loopback, real MCP client) | Done in tests: `test_streamable_http_client_over_loopback` |
| Docker image build, Lambda emulator, Lambda Web Adapter | **Not done.** No image has been built |
| Real AWS deployment | **Not done** |
| Live AWS through MCP | **Not done.** Live mode is tested with mocked botocore only |

## Bearer tokens

Generate each token (operator and Alexa) and its digest:

```bash
python -m cloudguard.deploy --new-token
```

- Give the token to the MCP client only. Configure only its SHA-256 digest on the server; the token itself is never stored or logged.
- The server sees only digests, so it **can't measure a token's strength**. It refuses the digests of a short list of known weak tokens (for both `CLOUDGUARD_TOKEN_SHA256` and `CLOUDGUARD_ALEXA_TOKEN_SHA256`) as a backstop, and `token_digest()` refuses tokens shorter than 43 URL-safe characters. Use `--new-token`.

## Request path

```
Function URL ─▶ Lambda Web Adapter 1.1.0 ─▶ uvicorn 127.0.0.1:8080 ─▶ BearerAuthApp ─▶ MCP SDK host/origin check ─▶ tool authorizer ─▶ tool
                                                                      │ 401 / 403 / 413        │ 421 / 403                    │ tool error
                                                                      └ /healthz (no auth)
```

| Layer | Decides | Where |
|---|---|---|
| Caller authentication | Who is calling | `Authenticator` in `cloudguard/deploy/http_app.py`. Implemented: one static bearer token, held as a SHA-256 digest and compared in constant time. |
| Tool authorization | Whether that caller may call a given tool | `authorize_tool`, enforced in `CloudGuardMCP.call_tool` before any argument handling or tool code. Cost tools need `cost:read`; governance tools need `governance:read`. |
| AWS IAM | Which AWS operations the process can perform | The Lambda execution role. Every tool is read-only. No tool argument can name an account, role, profile, region or credentials. |

**Authentication happens before any AWS call:**
- At startup in live mode, the app only builds boto3 clients. It makes no STS call, no `ec2:DescribeRegions` call and no scan.
- The enabled regions are listed inside the first governance scan, and a scan runs only inside an authorized tool call.
- Tests confirm this by recording every botocore call (`tests/test_lambda_prototype.py`).

## Configuration (environment)

| Variable | Meaning |
|---|---|
| `CLOUDGUARD_MODE` | `fixture` (default, synthetic data) or `live` (execution-role credentials) |
| `CLOUDGUARD_AUTH_MODE` | `static-token` (default). `oauth` is refused at startup: it is not implemented (see the blockers below). |
| `CLOUDGUARD_TOKEN_SHA256` | Hex SHA-256 of the bearer token. Required. The token itself is never stored or logged. |
| `CLOUDGUARD_TOKEN_SUBJECT` | Caller name recorded in request logs (default `static-token`) |
| `CLOUDGUARD_TOKEN_SCOPES` | `cost:read governance:read` by default. Narrow it to limit the token. |
| `CLOUDGUARD_ALEXA_TOKEN_SHA256` | Optional hex SHA-256 of a second token for the Alexa client (see "Alexa-scoped surface"). Must differ from the operator token's digest. |
| `CLOUDGUARD_ALLOWED_HOSTS` | Allowed `Host` values, for example the Function URL host. The default allows localhost only. `*` is refused. |
| `CLOUDGUARD_POLICY_STORE` | Read-only local policy directory. If it is absent, the built-in default policy is used. |
| `CLOUDGUARD_FIXTURE` | Cost fixture name or path (fixture mode only) |

**Adapter settings:**
- Set `AWS_LWA_READINESS_CHECK_PATH=/healthz`, `AWS_LWA_INVOKE_MODE=buffered` and `PORT=8080`.
- For a zip package, set `AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap` and use layer `arn:aws:lambda:<region>:753240598075:layer:LambdaAdapterLayerX86:30`. This is adapter 1.1.0; use `LambdaAdapterLayerArm64:30` on arm64.
- The image build pins `public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0@sha256:17cfd08eff1dfea3f6a9a1e9c65fdac80aa4919b6085e746615530f43f57d2f1`.

## Running locally

```bash
CLOUDGUARD_TOKEN_SHA256=<digest> python -m cloudguard.deploy --port 8080
```

Run from `backend/`. It binds to `127.0.0.1` only. `curl http://127.0.0.1:8080/healthz` returns `{"status":"ok","mode":"fixture"}`; MCP clients use Streamable HTTP at `http://127.0.0.1:8080/mcp` with `Authorization: Bearer <token>`.

## HTTP behaviour

| Request | Response |
|---|---|
| `GET /healthz` | 200 `{"status":"ok","mode":...}`. No auth, no AWS call. |
| Missing, malformed or wrong bearer token | 401 `{"error":"unauthorized"}`, `WWW-Authenticate: Bearer realm="cloudguard"` |
| Valid token with no CloudGuard scope | 403 `{"error":"forbidden"}`, `WWW-Authenticate: ... error="insufficient_scope"` |
| Valid token, body larger than 64 KiB | 413 `{"error":"payload_too_large"}` (the MCP SDK's own 4 MiB limit also applies) |
| Valid token, malformed JSON body | 400 JSON-RPC parse error from the MCP SDK |
| Valid token, `Host` not allowed | 421 (MCP SDK DNS-rebinding protection) |
| Valid token, foreign browser `Origin` | 403 (MCP SDK) |
| Valid token missing the tool's scope | JSON-RPC result with `isError: true`, "not authorized to call …" |
| `GET /.well-known/oauth-protected-resource` | 404. OAuth is not implemented, so no metadata is advertised. |
| Unexpected server error | 500 `{"error":"internal_error"}`. No stack trace or details. |

AWS error messages never reach responses: tool results carry only the error kind, code and operation, as in the governance hardening.

## Lambda behaviour and limits

- **Timeouts:**
  - A live governance scan took about 101 seconds over 18 regions on 2026-10-20.
  - Set the function timeout to at least 300 seconds. A Function URL allows up to the function timeout, which can be as long as 900 seconds.
  - Behind API Gateway, the 29–30 second integration limit would cut a cold scan off.
  - MCP clients, and especially a voice assistant, will likely time out well before 101 seconds. See the blockers below.
- **Payload size:** 64 KiB per request in the app. Lambda Function URLs have their own payload limit (about 6 MB per AWS documentation; not verified here).
- **Concurrency:**
  - Lambda gives each execution environment one request at a time, so every cold environment runs its own scan, and the 5-minute scan cache lives per environment.
  - Use reserved concurrency (for example 1–2) to limit parallel scans and AWS API load.
  - Inside one environment, the governance coordinator runs at most one scan, on one worker thread.
- **Cold start:**
  - Startup imports mcp, pydantic and starlette; live mode also imports boto3. It makes no network calls.
  - The first governance call pays for region discovery and the scan.
- **Threads:** scans run in the request's worker thread and finish before the response is sent. Nothing runs in the background after a response, so Lambda's freeze between invocations is safe.
- **Filesystem:**
  - The code runs from read-only `/var/task`, and the server never writes.
  - The policy store is only read. It is absent from the image, because `.dockerignore` excludes `.cloudguard`, so the built-in default policy applies.
  - `PYTHONDONTWRITEBYTECODE=1` is set.
- **Stateless MCP:**
  - `stateless_http=True` and `json_response=True`, so every POST stands alone and no `mcp-session-id` is issued.
  - Initialization is per request and nothing is pinned to an execution environment.
  - Responses are JSON, which suits buffered invoke mode.

## Blockers before a real deployment

1. **OAuth 2.1 for Alexa+ account linking is not implemented.** It needs:
   - an authorization server: Login with Amazon, Amazon Cognito or another provider;
   - its issuer URL, JWKS or introspection endpoint, the expected audience or resource, and scopes;
   - client registration (static or dynamic) and the Alexa+ redirect URIs;
   - confirmation of exactly what the Alexa+ MCP integration requires. This is not documented in this repo.

   Once those are known, a `TokenVerifier`-style `Authenticator` and `/.well-known/oauth-protected-resource` (RFC 9728) metadata can be added behind the existing interface. Until then, only the operator's static bearer token works, which suits a controlled experiment but not account linking.
2. **Scan latency versus client timeouts.** About 101 seconds per cold scan is too slow for an interactive voice reply. Options include warming the cache with a scheduled invocation, an asynchronous "start scan, then ask for results" flow, or scanning fewer regions. This is a product decision.
3. **Execution role.** It needs the read-only discovery permissions (`docs/aws-permissions.md`) plus Cost Explorer read access if the cost tools are used live. The role is not created here, and IAM was not changed.
4. **Not exercised:** the image build, the zip packaging and the adapter itself. Tests drive the ASGI app directly, the way the adapter forwards requests, with no Docker and no Lambda emulator.

## Alexa-scoped surface (`alexa:read`)
Set `CLOUDGUARD_ALEXA_TOKEN_SHA256` to the SHA-256 of a second, separate bearer token for the Alexa client. Its caller has the subject `alexa` and only the `alexa:read` scope.
- **Tools:** it may list and call exactly the twelve read-only tools in `ALEXA_TOOLS` (`cloudguard/deploy/http_app.py`). The list is explicit, never derived from what is registered.
- **Two separate checks:** `tools/list` is filtered for it, and `tools/call` re-checks every call before any argument handling or AWS call.
- **Batches:** JSON-RPC batches are refused by the MCP SDK (400).
- **No mixed scopes:** `alexa:read` can't be combined with other scopes.
- **Startup check:** startup fails if an Alexa tool is missing or not annotated read-only.
- **Operator token unchanged:** its scopes, calls and listing are as before.

This is still a static token. OAuth 2.1 account linking is **not** implemented.

### Alexa response projection (data minimisation)
**Speech sanitization is not enough.** Every tool's `speech` field already avoids names, values, ARNs and account IDs. But an MCP client can read the whole result: the `structuredContent`, and the text block, which is a JSON copy of it. So Alexa never receives the operator result at all.

**The boundary:** `cloudguard/deploy/alexa_projection.py` and `CloudGuardMCP.call_tool`.
- **Who:** the projection is chosen from the authenticated principal (`alexa:read`), never from anything in the request.
- **When:** it runs after authorization and tool execution.
- **What it replaces:** both the structured content and the text content (the text is a JSON copy of the projected answer).
- **How it is built:** the Alexa answer copies named, safe fields out of the operator result (an allowlist), so a field added to an operator contract later is never sent to Alexa by accident.
- **Fails closed:** a tool without a projection, or an unexpected shape, is an error.
- **Schemas:** Alexa's `tools/list` advertises the projected output schemas (`schema_version: "alexa-1"`).
- **Errors:** Alexa's error text is sanitized. Argument errors become "invalid arguments for TOOL", and nothing the caller sent is echoed back.

**What Alexa receives:**
- tool, outcome, mode, speech, and freshness (generated and observed time, reused, estimated);
- cost tools:
  - currency, dates, period labels;
  - bill totals (charges, usage, fees, tax, discounts, credits, refunds, calculated net);
  - the top 5 AWS services;
  - month-over-month changes, the verdict, and the top 5 drivers and offsets;
  - the attribution limitation note;
  - anomaly window, monitor count and the top 5 anomalies (service, region, dates, amounts);
  - warning codes, and the AWS error code only;
- governance tools:
  - scanned regions, counts by service and region, coverage, error counts;
  - compliance totals and breakdowns, missing-key counts, invalid-value counts by key and reason (never the values);
  - per finding: the opaque `finding_id`, service, region, resource type, status, missing and invalid keys, the evidence API and time, the AWS error kind and code, and CloudGuard's template explanation;
  - recommendations: status, reason, confidence and accepted evidence *types*. A value is shown only when it is one of the policy's own allowed values; otherwise `value_withheld: true`.

**Never sent to Alexa:**
- account IDs, principal and resource ARNs, resource IDs and names;
- observed, current, invalid or free-form recommended tag values;
- operator evidence values, sources, hash and problems;
- policy identity and store, `data_source`, file paths, scan IDs;
- AWS error messages and descriptions, and CloudGuard's inferred causes;
- rule descriptions and aliases.

**Operator responses are unchanged.** With a projection configured, every result is returned as a `CallToolResult`, so the low-level server's shared tool-schema cache can't apply Alexa's schemas to operator calls. FastMCP has already validated each result against its own model.

**Still open:**
- **Static tokens:** both tokens are static bearer tokens, with no expiry, rotation or per-user identity.
- **No OAuth:** OAuth 2.1 / Alexa+ account linking is not implemented.
- **Shared visibility:** anyone holding the Alexa token sees the projected data for the whole account.
- **Resource identity:** service, region and resource type, together with `finding_id`, can still identify a resource to someone who already knows the account.
