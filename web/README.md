# CloudGuard MCP Web Simulator

A browser **MCP client** for the CloudGuard HTTP MCP server, built for the hackathon demo. It shows two things side by side: what a voice assistant would say (the tool's `speech`), and the structured evidence an MCP client receives.

> **This is not Alexa+.** It is a custom MCP client that authenticates with CloudGuard's *Alexa-scoped* token, so you see exactly the surface an Alexa+ integration would get. There is no Amazon account linking, no OAuth and no Alexa service involved.

## Architecture
```
Browser (React + TS, @modelcontextprotocol/sdk client)
   │  same origin: http://127.0.0.1:5173/mcp   (Authorization: Bearer <Alexa token>)
Vite dev server ── proxy /mcp, /healthz (adds nothing, forwards the header unchanged)
   │  http://127.0.0.1:8080/mcp
CloudGuard deployment prototype (python -m cloudguard.deploy)
   bearer auth → alexa:read allowlist (12 tools) → tool → Alexa response projection
```

**Transport:** the official MCP TypeScript SDK (`StreamableHTTPClientTransport` and `Client`) does the protocol work:
- `initialize`, offering protocol `2025-11-25` and accepting what the server answers;
- `notifications/initialized`;
- the `MCP-Protocol-Version` header on later requests;
- `tools/list` (with pagination) and `tools/call`.

The CloudGuard server is **stateless** (no `Mcp-Session-Id`) and answers with JSON. The simulator reports both in the header bar.

**Why a proxy:** the backend has **no CORS**: a cross-origin preflight gets 401, with no `Access-Control-*` headers. Its DNS-rebinding protection only accepts loopback origins. So the browser talks to the Vite dev server on the same origin, and Vite forwards `/mcp` and `/healthz`. The proxy has no credentials of its own; the backend still authenticates and authorizes every request. No backend change was made.

**No local logic:**
- Every result shown comes from `tools/call`.
- The tool list comes from `tools/list`; only the sidebar grouping is cosmetic.
- The "Ask CloudGuard" box maps questions to *discovered* tools with fixed keyword rules (`src/lib/intent.ts`). There is no AI model and nothing is sent anywhere.
- It never guesses IDs: when a question needs a finding or resource, it asks for one. Read-only tools (all 12 today) run as soon as the question is understood; any tool that is not both a known CloudGuard read-only tool and declared read-only by the server waits for an explicit confirmation.

## Prerequisites
- Node.js 20+, npm.
- The backend virtualenv: `backend/.venv`, Python 3.14; see the root README.

## Install
```bash
cd web
npm install
```
On this Windows dev machine `npm install` failed until `COMSPEC` was set (esbuild's postinstall needs a shell). In PowerShell: `$env:COMSPEC = "C:\Windows\System32\cmd.exe"`.

## Demo setup (fixture mode: synthetic data, no AWS credentials, no AWS calls)
**1. Make two throwaway tokens.**
- Run this twice: once for the operator token (required by the server, never used in the browser) and once for the Alexa token.
- Each run prints the token, then its SHA-256.
- Keep the Alexa token for the browser. Do not commit either.
```bash
python -c "import secrets,hashlib;t=secrets.token_urlsafe(32);print(t);print(hashlib.sha256(t.encode()).hexdigest())"
```

**2. Start the backend in fixture mode** (PowerShell, from `backend/`). Put the two digests in the environment:
```powershell
$env:CLOUDGUARD_MODE = "fixture"
$env:CLOUDGUARD_TOKEN_SHA256 = "<operator token sha256>"
$env:CLOUDGUARD_ALEXA_TOKEN_SHA256 = "<alexa token sha256>"
.venv\Scripts\python.exe -m cloudguard.deploy --port 8080
```
`GET http://127.0.0.1:8080/healthz` should answer `{"status": "ok", "mode": "fixture"}`.

**3. Start the simulator** (from `web/`):
```bash
npm run dev
```
To proxy elsewhere, copy `.env.example` to `.env` and set `CLOUDGUARD_MCP_TARGET`. Never put a token in it.

**4. Connect.**
- Open http://127.0.0.1:5173.
- Leave the URL as `/mcp`, paste the **Alexa** token, and click **Connect**.
- The header shows the protocol version, "stateless", and a purple **FIXTURE MODE** badge. Fixture results also carry a **FIXTURE DATA** tag.

## Fixture versus live
- **Fixture** (`CLOUDGUARD_MODE=fixture`): synthetic data from `backend/fixtures/`, no AWS credentials, no AWS API calls.
- **Live** (`CLOUDGUARD_MODE=live`): read-only calls with the server process's AWS credentials, labelled **LIVE MODE · real AWS account**. Live mode was not exercised in this phase.

## Supported tools
Whatever `tools/list` returns for the token. For the Alexa token, that is the twelve read-only tools:
- **Cost:** `get_monthly_aws_bill`, `get_current_month_spending`, `get_cost_breakdown`, `compare_monthly_costs`, `investigate_cost_increase`, `get_cost_anomalies`.
- **Governance:** `get_resource_inventory`, `scan_tag_compliance`, `list_untagged_resources`, `explain_finding`, `recommend_tags`.
- **Account:** `get_aws_account_identity`.

## Security boundaries
- **Token in memory only:**
  - It is held in the connection's closure.
  - The input field is cleared right after connecting.
  - It is sent only as the `Authorization` header, with `credentials: "omit"`.
  - It is never written to browser storage or cookies, never logged, and scrubbed from error text.
  - Plain `http` to a non-loopback host is refused.
- **Alexa only:** the simulator refuses a connection whose tools don't advertise the Alexa projection (`schema_version: "alexa-1"`), so an operator token can't be used here by mistake.
- **Server-side enforcement:** the allowlist, scopes and data minimisation are all enforced by the server. The UI can't widen them: hidden tools stay uncallable, and responses are already projected.
- **Input validation:** input is checked against the advertised `inputSchema` before any request (required fields, JSON types, ranges, patterns, unknown arguments, nested objects and arrays). The server validates again.
- **No fabricated data:** failures show the HTTP, MCP or tool error and **no result**. Charts are drawn only from numbers in the response.

## Demo walkthrough (about 2 minutes)
1. **Connect** with the Alexa token. The sidebar shows "12 tools returned by tools/list", all marked read-only (RO).
2. **Ask** "What was my AWS bill last month?". The plan shows `get_monthly_aws_bill {"month":"last-month"}` and the call runs at once (read-only, so no confirmation).
3. **Separate panels:** the *Voice response* panel shows the speech; the *Structured evidence* panel shows totals, period and top services; *Raw MCP result* is the exact `CallToolResult`.
4. **Ask** "Why did my AWS cost increase?" to run `investigate_cost_increase`, showing the drivers and the attribution limitation.
   **Ask** "Show me my AWS resources." With no region named, CloudGuard counts every enabled region (the server's discovery stays bounded). "Show my Lambda functions in Mumbai" scans only Lambda in ap-south-1. A region it doesn't know ("in Atlantis") or one that fits several ("US East") is asked about, never treated as all regions; answer "Mumbai", "ap-south-1" or "all".
   **Ask** "List all the Lambdas which are untagged." (speech recognition's "unpacked" also works next to a resource word), then "Why is legacy-report-generator untagged?" and "What tags should I add?". Resources are named, not identified by ARN or finding ID: "Why is my Lambda function non-compliant?" asks which one and offers names. Names come from the server as `display_name`, and only when they are plain (letters, digits, `.`, `_`, `-`); any other name is withheld and never read out.
5. **Ask** "Check my tagging compliance." to run `scan_tag_compliance`, showing counts by service and region, without resource names.
6. **Ask** "Show me untagged resources." It returns opaque finding IDs, not ARNs.

**Voice (Chrome or Edge):** tap the microphone and say "How much did AWS cost last month?". CloudGuard shows the transcript, makes exactly one read-only tool call, and speaks the tool's `speech` field. It does not ask "Shall I continue?" for read-only tools. A tool that is not read-only (none is on CloudGuard's MCP surface today; applying tags would be one) is described and held: say "yes", "yes, proceed", "confirm" or "go ahead" (or press **Yes, run it**) to run it once; "no", "cancel" or "don't do it" runs nothing, a refusal always wins over a yes, and any unclear or empty answer runs nothing and asks again. A group tagging request with a name filter ("Find all Lambdas with codebridge in their names and tag them") starts a tagging conversation (`src/lib/tagflow.ts`): `search_resources` in the named region or, when none is named, the **Selected region** (default Mumbai, ap-south-1); the matches and count; a question for the key and value ("Key is owner, value is mukesh"); the complete operation repeated back; and an explicit yes or no. Only a plain yes to the summary just given runs `preview_tag_changes` (a read-only dry run); "sounds good", "that's fine", silence and qualified answers never do, and any new key or value replaces the summary so an earlier yes no longer counts. The preview's saveable request is shown with the CLI steps: an operator saves it, a different person approves its content hash, and someone who is not the approver executes it, which verifies and reports every resource. Nothing said to the simulator saves, approves or applies anything. Other tag-change requests are explained and never routed to a tool. **Personal voice tagging** (off by default; see `docs/voice-tagging.md`): when the server enables it and you sign in with the separate operator credential in the sidebar (an HttpOnly cookie; no script sees it), a *spoken* tagging request goes on past the preview: CloudGuard reads back the full plan from live discovery, and after a spoken yes (plus "I confirm the overwrite" for replaced values) the server applies the tags through the existing approval and execution workflow and reports each resource. Typed chat stays preview-only. **Replay**, **Stop speaking** and **Mute** are under the voice panel.

**Hands-free (Auto-listen, on by default):** after an answer CloudGuard asks "Would you like to know anything else?" and listens for the next question. When a call needs confirmation, it listens for your yes or no after the prompt ("Listening for confirmation"), and every such call needs its own yes. Listening stops after 8 seconds of nothing usable ("Didn't hear you…", with **Try again**). Untick **Auto-listen** to tap the microphone each time instead.
7. **Ask** "Explain this finding." The simulator asks which one. Open the tool, pick a returned finding, and call it.
8. **Ask** "Recommend tags for this resource." Same flow. Policy-canonical values are shown; free-form values are withheld (`value_withheld`).
9. **Optional:** in raw JSON mode, send a foreign-account ARN to see a sanitized tool error, or stop the backend to see a request failure with no result.

## Tests
```bash
cd web
npm test
```
`npm test` covers intent mapping, schema validation, error handling, and static security checks (no storage, no logging, token only in the header, no AWS or AI URLs, no hardcoded results). It also covers the voice flow with the browser speech APIs replaced by fakes: speech recognition and synthesis (`voice`, `listening`), the spoken yes/no confirmation parser (`confirm`), hands-free conversation turns (`conversation`), which calls run at once and which need a yes (`readonly-confirmation`), voice selection (`voices`), spoken-text consistency (`speech-consistency`), and the in-memory answer cache (`cache`).

```bash
npm run typecheck
```

Backend transport pins, from `backend/`:
```bash
.venv\Scripts\python.exe -m pytest tests/test_simulator_transport.py
```

## Automated end-to-end tests (Playwright)
```bash
cd web
npx playwright install chromium
npm run test:e2e
```
The first command is a one-time download.

`playwright.config.ts` starts **its own** fixture backend (port 8091, through `e2e/fixture_backend.py`) and Vite server (port 5174), so it never reuses a running demo. Each run uses fresh random tokens, held in environment variables only.

Nothing is mocked: every result comes from the real server through the real proxy. `e2e/simulator.spec.ts` covers:
- the disconnected state;
- a wrong token (401) and the operator token (refused);
- connection, the fixture banner, and exactly twelve tools in three groups (Cost Intelligence, Resource Governance, Account);
- `get_monthly_aws_bill` (speech and evidence), `get_resource_inventory`, `scan_tag_compliance`, `list_untagged_resources`, and `recommend_tags` with a server-returned finding ID;
- malformed JSON and an unknown `account_id` argument, both rejected with **no** network request;
- a direct `approve_fix` call with the Alexa token, refused by the server;
- projected responses containing no account ID, ARN or excluded fields;
- disconnect clearing the token (DOM, inputs, URL, storage, cookies);
- the token never appearing in console output or request bodies;
- no horizontal scroll at 1366×768;
- **zero AWS API calls**: the backend's botocore tripwire log must be empty.

`e2e/voice.spec.ts`, `e2e/cache.spec.ts` and `e2e/identity.spec.ts` cover the voice flow, answer-cache reuse with Refresh and reconnect, and the identity question. The voice tests install fake browser speech APIs; the MCP calls still go to the real fixture backend.

## Live-mode smoke test (manual; never automated)
Run this only on purpose, by the account owner. It is **read-only**: no AWS resource, tag or IAM change. CloudGuard never grants itself permissions.

**1. Permissions (the owner attaches them; CloudGuard doesn't).** The server's credentials need:
- the read-only actions in `infra/iam/cost-intelligence-policy.json`: `ce:GetCostAndUsage`, `ce:GetAnomalyMonitors`, `ce:GetAnomalies`;
- the actions in `infra/iam/governance-discovery-policy.json`: List and Describe calls, plus `ec2:DescribeRegions`.

The current developer CLI identity is known to **lack** `ce:*`. Without it, the cost tools answer `outcome: "unavailable"` with an AWS error code, never zero spend.

**2. Check the identity first:**
```bash
aws sts get-caller-identity
```
It should be the read-only identity you expect, not an admin.

**3. Start the backend in live mode** (PowerShell, from `backend/`). `AWS_PROFILE` selects the credentials. Use a *different* Alexa token from the fixture demo:
```powershell
$env:CLOUDGUARD_MODE = "live"
$env:AWS_PROFILE = "<PROFILE>"
$env:CLOUDGUARD_TOKEN_SHA256 = "<operator sha256>"
$env:CLOUDGUARD_ALEXA_TOKEN_SHA256 = "<alexa sha256>"
.venv\Scripts\python.exe -m cloudguard.deploy --port 8080
```
Then `npm run dev`, and connect.

**4. Confirm it is live:**
- the amber **LIVE MODE** banner;
- **LIVE AWS DATA** tags on results;
- `mode: "live"` in the evidence;
- `/healthz` reporting `live`.

If the banner says **MODE UNKNOWN**, stop.

**5. Call each tool once.** Cost Explorer costs about **$0.01 per request**. The server keeps no cache across cost calls. The simulator keeps an in-memory answer cache for the current connection only: 5 minutes for completed-month bill, breakdown, comparison and increase answers, and 2 minutes for current-month spending and anomalies. A repeated question within that time is answered from the cache, marked "Cached response", with no server or AWS call; **Refresh data** calls the server again. The cache is cleared on disconnect and never written to browser storage.

| Tool | Cost Explorer requests (`freshness.aws_requests`) |
|---|---|
| `get_monthly_aws_bill` | 1 |
| `get_current_month_spending` | 1 |
| `get_cost_breakdown` | 1 |
| `compare_monthly_costs` | 1 |
| `investigate_cost_increase` | 1 |
| `get_cost_anomalies` | 2 when a monitor exists, else 1 |

One pass is about 7 requests (about $0.07). Don't click repeatedly.

Governance tools are free list and describe calls, and share one scan for 5 minutes.

**6. What to check:**
- **Completed months:** `is_final_bill: true`, and the period label is the previous UTC month.
- **Current month:** `is_final_bill: false`, with `MONTH_TO_DATE` and usually `ESTIMATED` in `warnings`. The speech says "so far". The period ends today (UTC, exclusive).
- **Anomalies:** with no anomaly monitor, the status is `NO_MONITOR` and the speech says no monitor is set up. That is not "no anomalies". `NOT_YET_EVALUATED` and `STALE_EVALUATION` are also possible.
- **Governance:** `regions_scanned` lists the enabled regions, and `coverage_complete: false` if any service listing was denied.

**7. Finish:** stop the server and remove the two environment variables. Live results are never stored by the simulator.

## Known limitations
- **Static token:** no OAuth or account linking, and no token expiry or rotation.
- **Development only:** the same-origin proxy is the Vite dev or preview server. A production deployment would need the backend, or a gateway, to serve the app or allow a specific origin.
- **Keyword matching:** intent rules are simple and English-only. Unmatched or ambiguous questions fall back to choosing a tool.
- **Speech loudness:** every utterance is sent with volume 1, rate 1 and pitch 1 and the same selected voice. Perceived loudness still depends on the voice engine: Windows (SAPI) voices, Edge's online "Natural" voices and Chrome's "Google" voices are mastered at different levels, and Windows may lower ("duck") other audio while the microphone is open. Volume 1 is the maximum the Web Speech API allows; it can't boost a quiet voice.
- **Hands-free listening:** Chrome and Edge ask for microphone permission once per site, and only on `localhost`/`127.0.0.1` or HTTPS. Chrome ends a session on its own after a few seconds of silence ("no-speech"), sometimes before the 8-second timeout. Recognition runs on Google's (Chrome) or Microsoft's (Edge) servers, so it needs a network connection. With speakers rather than headphones, the microphone can pick up CloudGuard's own voice; listening starts only after speech ends (plus a short gap), but echo can still be transcribed. Firefox has no speech recognition.
- **Browser voice only:** voice input uses the browser's Web Speech API (`SpeechRecognition` / `webkitSpeechRecognition`), so it works in Chrome and Edge; Firefox has no recognition, and Safari's is unreliable. Chrome sends the audio to Google's speech service to transcribe it; the simulator itself never records, uploads or stores audio. Spoken replies use `speechSynthesis`. There is no wake word: tap the microphone. This is a browser simulation, not an Alexa+ integration.
