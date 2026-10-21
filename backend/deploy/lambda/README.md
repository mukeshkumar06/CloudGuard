# CloudGuard cost MCP on AWS Lambda: deployment prototype

**Status:** prototype. **Not deployed.** Nothing here creates AWS resources.

The prototype runs the CloudGuard cost MCP server (the six read-only cost tools in `docs/cost-intelligence.md`) as a Streamable HTTP app behind [AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter). A Lambda Function URL request reaches the adapter, which forwards it to uvicorn on `127.0.0.1:$PORT`. From there it goes to `cloudguard.deploy.http_app` and then to the MCP server.

## What has and hasn't been verified

| Level | Status |
|---|---|
| Offline tests (ASGI test client, fixtures, mocked botocore) | Done: `tests/test_http_deploy.py`, `tests/test_deploy_scopes.py` |
| Local HTTP server (uvicorn on loopback, real MCP client) | Done in tests: `test_streamable_http_client_over_loopback` |
| Docker image build, Lambda emulator, Lambda Web Adapter | **Not done.** No image has been built |
| Real AWS deployment | **Not done** |
| Live Cost Explorer through MCP | **Not done.** Live mode is tested with mocked botocore only |

## Request path

```
Function URL ─▶ Lambda Web Adapter 1.1.0 ─▶ uvicorn 127.0.0.1:8080 ─▶ BearerAuthApp ─▶ MCP SDK host/origin check ─▶ tool authorizer ─▶ cost tool
                                                                      │ 401 / 403 / 413        │ 421 / 403                    │ tool error
                                                                      └ /healthz (no auth)
```

| Layer | Decides | Where |
|---|---|---|
| Caller authentication | Who is calling | `StaticTokenAuthenticator` in `cloudguard/deploy/http_app.py`: one static bearer token, held as a SHA-256 digest and compared with `hmac.compare_digest`. |
| Tool authorization | Whether that caller may call a tool | `authorize_tool`, enforced in `CloudGuardMCP.call_tool` before any argument handling or tool code. Every cost tool needs `cost:read`. Unknown tools are refused. |
| AWS IAM | Which AWS operations the process can perform | The Lambda execution role. Every tool is read-only. No tool argument can name an account, role, profile, region or credentials. |

**Authentication happens before any AWS call.** At startup in live mode, the app only builds the Cost Explorer client; it calls AWS only inside an authorized tool call. Tests confirm this by recording every botocore call.

**stdio is unaffected.** `python -m cloudguard.server` (stdio or local HTTP) has no authorizer and behaves as in C3.

## Bearer token

Generate a random token of 32 bytes and its digest:

```bash
python -m cloudguard.deploy --new-token
```

- Give the token to the MCP client only. Configure only `CLOUDGUARD_TOKEN_SHA256` on the server; the token itself is never stored or logged.
- The server sees only the digest, so it **can't measure a token's strength**. It refuses the digests of a short list of known weak tokens as a backstop, and `token_digest()` refuses tokens shorter than 43 URL-safe characters. Use `--new-token`.
- Rotate by generating a new token and updating the digest; the old token stops working at the next start.

## Configuration (environment)

| Variable | Meaning |
|---|---|
| `CLOUDGUARD_MODE` | `fixture` (default, synthetic data) or `live` (execution-role credentials) |
| `CLOUDGUARD_AUTH_MODE` | `static-token` (default). `oauth` is refused at startup: it is not implemented. |
| `CLOUDGUARD_TOKEN_SHA256` | Hex SHA-256 of the bearer token. Required. |
| `CLOUDGUARD_TOKEN_SUBJECT` | Caller name recorded in request logs (default `static-token`) |
| `CLOUDGUARD_TOKEN_SCOPES` | `cost:read` by default; the only known scope. A token without it gets 403. |
| `CLOUDGUARD_ALLOWED_HOSTS` | Allowed `Host` values, for example the Function URL host. The default allows localhost only. `*` is refused. |
| `CLOUDGUARD_FIXTURE` | Cost fixture name or path (fixture mode only) |

**Adapter settings:**
- Set `AWS_LWA_READINESS_CHECK_PATH=/healthz`, `AWS_LWA_INVOKE_MODE=buffered` and `PORT=8080`.
- For a zip package, set `AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap` and use layer `arn:aws:lambda:<region>:753240598075:layer:LambdaAdapterLayerX86:30` (adapter 1.1.0; `LambdaAdapterLayerArm64:30` on arm64).
- The image build pins `public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0@sha256:17cfd08eff1dfea3f6a9a1e9c65fdac80aa4919b6085e746615530f43f57d2f1`.
- Dependencies are pinned in `requirements.txt`.

## Running locally

```bash
CLOUDGUARD_TOKEN_SHA256=<digest> python -m cloudguard.deploy --port 8080
```

Run from `backend/`. It binds to `127.0.0.1` only.

- **Health check:** `curl http://127.0.0.1:8080/healthz` returns `{"status":"ok","mode":"fixture"}`.
- **MCP client:** Streamable HTTP at `http://127.0.0.1:8080/mcp`, with the header `Authorization: Bearer <token>`.

## HTTP behaviour

| Request | Response |
|---|---|
| `GET /healthz` | 200 `{"status":"ok","mode":...}`. No auth, no AWS call. |
| No or invalid `Authorization` header | 401 `{"error":"unauthorized"}`, `WWW-Authenticate: Bearer realm="cloudguard"` |
| Valid token without `cost:read` | 403 `{"error":"forbidden"}`, `error="insufficient_scope"` |
| Body larger than 64 KiB | 413 `{"error":"payload_too_large"}` (the MCP SDK's own 4 MiB limit also applies) |
| Malformed JSON body | 400 JSON-RPC parse error from the MCP SDK |
| `Host` not allowed | 421 (MCP SDK DNS-rebinding protection) |
| Foreign browser `Origin` | 403 (MCP SDK) |
| Unknown tool, or unknown arguments | JSON-RPC result with `isError: true` |
| WebSocket | Closed with code 1008 |
| `GET /.well-known/oauth-protected-resource` | 404. OAuth is not implemented. |
| Unexpected server error | 500 `{"error":"internal_error"}`. No details. |

There is no CORS support: a browser client on another origin needs a same-origin proxy.

## IAM (execution role)

The cost tools need only `infra/iam/cost-intelligence-policy.json`: `ce:GetCostAndUsage`, `ce:GetAnomalies` and `ce:GetAnomalyMonitors`, all read-only. Plus the usual CloudWatch Logs permissions for a Lambda function. No role is created here, and IAM has not been changed.

## Limits

- **Request timeout:** the app sets none; the Lambda function timeout bounds each request. A cost tool makes 1–2 Cost Explorer requests (up to 5 pages each by default). A function timeout of at least 60 seconds is our **estimate**, not a measurement.
- **Payload size:** 64 KiB per request in the app. Lambda Function URLs have their own payload limit (about 6 MB per AWS documentation; not verified here).
- **Concurrency:** Lambda gives each execution environment one request at a time. Use reserved concurrency to cap parallel Cost Explorer calls (about $0.01 each).
- **Cold start:** imports mcp, pydantic and starlette; live mode also imports boto3. No network calls at startup.

## Blockers before a real deployment

1. **OAuth 2.1 for Alexa+ account linking is not implemented**, and Alexa+ is not connected. It needs an authorization server and the Alexa+ requirements for issuer, audience, scopes and redirect URIs, which aren't documented in this repository.
2. **Execution role** with the read-only cost policy, not created here.
3. **Not exercised:** the image build, zip packaging, the adapter, the Lambda Python 3.14 base image and a Function URL.
