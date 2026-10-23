# Personal voice tagging

One operator tags resources in their own AWS development account by voice: CloudGuard finds the resources, asks
for the tag, reads the whole operation back, and applies and verifies it after a spoken yes. Nobody runs the
tagging CLI.

**Status:** implemented and tested offline only: mocked AWS (`FakeAws`, botocore-shaped fakes) and the simulated
fixture account. **Not yet verified against real AWS**, a real tagging role or a real microphone. Disabled by
default.

## When to use it, and when not

Only for a personal development account where you are the only person. It deliberately waives separation of
duties (requester, approver and executor are the same person) for the accounts listed in
`CLOUDGUARD_VOICE_TAGGING_ACCOUNTS`, and only on this path. Team and production accounts keep the normal workflow
(`docs/live-tagging.md`): a different person approves, someone other than the approver executes. Never list such an
account here.

## Flow

| Step | Where | Writes |
|---|---|---|
| Operator signs in with the dedicated credential (form post) | `POST /operator/login` → HttpOnly, SameSite=Strict cookie on `/operator` | no |
| "Find all Lambda functions containing CodeBridge in Mumbai and tag them." | MCP `search_resources` (read-only): count and names | no |
| "Key is owner, value is mukesh." | `POST /operator/voice-tagging/prepare`: STS identity, account allow-list, live discovery, the plan, a confirmation challenge | no |
| CloudGuard reads back service, region, filter, every resource, key, value and every overwrite | browser | no |
| "Yes" (plus "I confirm the overwrite" when values are replaced) | `POST /operator/voice-tagging/confirm` | **yes**, after the checks below |
| Per-resource result: tagged and verified / already correct / failed / skipped | server speech | no |

## What a spoken yes authorizes, and the checks before any write

- **Only the exact plan just read back.** The challenge is random, bound to the operator session, expires after
  2 minutes and carries a plan hash over the account, region, request and every resource's current tags and
  changes. A new key, value, service, region or filter cancels it and prepares a new one.
- **The server reads the transcript itself** (`voice_tagging/confirm.py`): only an exact affirmative phrase
  ("yes", "proceed", "go ahead", "confirm" ...). "Sounds good", "that's fine", "ok", partial or empty transcripts
  are not confirmations. "No" cancels. Overwrites need a separate "I confirm the overwrite".
- **Once only.** The challenge is locked and moved to EXECUTING before anything else; a repeated transcript or a
  replayed request returns the stored result and never executes again. Executions also carry idempotency keys.
- **Re-resolved from AWS** immediately before execution: if any resource disappeared, appeared or changed tags,
  nothing is written ("PROPOSAL_CHANGED") and a new plan must be confirmed.
- **Identity and account:** the caller comes from `sts:GetCallerIdentity`, never from the browser; its account must
  be allow-listed, and the assumed tagging role must be in the same account.
- **The existing workflow does the rest:** `DecisionService` creates and approves the proposal (allow-list
  permissions, content hash, nonce, overwrite acknowledgement, policy) with a `PersonalAccountWaiver` for this one
  account, and `LiveRemediationExecutor` revalidates, re-reads each resource before writing, records a fenced
  write intent, writes, and verifies by reading back. Success is reported only after that read-back.
- **Typed chat can't write.** Only the voice controller calls these routes, and the server refuses any request
  without `channel: "voice"` and the `X-CloudGuard-Voice` header. (This is a client convention, not
  authentication: the operator session is what authorizes.)
- **Never through MCP.** No MCP tool saves, approves or executes; the Alexa and operator bearer tokens can't reach
  `/operator` at all.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `CLOUDGUARD_VOICE_TAGGING_ENABLED` | | `true` to enable. Anything else: disabled, `/operator` answers 404 |
| `CLOUDGUARD_VOICE_OPERATOR_TOKEN_SHA256` | when enabled | SHA-256 of the dedicated operator credential (different from both MCP tokens). Make one with `python -m cloudguard.deploy --new-token` and use only the printed digest here |
| `CLOUDGUARD_VOICE_TAGGING_ACCOUNTS` | when enabled | your development account ID(s), comma-separated |
| `CLOUDGUARD_VOICE_TAGGING_ROLE_ARN` | live mode | the tagging role the server assumes for writes (in an allow-listed account) |
| `CLOUDGUARD_VOICE_TAGGING_KILL_FILE` | | kill switch file (default `.cloudguard/voice-tagging.disabled`) |
| `CLOUDGUARD_VOICE_TAGGING_STORE` | | proposals, allow-list and audit log (default `.cloudguard`) |
| `CLOUDGUARD_VOICE_OPERATOR_SUBJECT` | | operator name in logs and audit (default `operator`) |
| `CLOUDGUARD_VOICE_SESSION_TTL_SECONDS` | | session lifetime, 60–14400 (default 1800) |

Live mode also needs a LIVE allow-list in `<store>/authz` (the CLI's store) that binds the server's own AWS
principal to you with `create`, `approve`, `reject` and `execute` for the account (`authz save --mode live`, see
`docs/authorization.md`). The voice path never creates or edits it.

## IAM (design only; nothing is deployed)

- **Server identity** (the credentials the server runs with): the existing read-only discovery permissions, plus
  `sts:AssumeRole` on the tagging role.
- **Tagging role:** `infra/iam/proposed/tagger-role-policy.json` (tag writes for the four types, read-back
  verification, explicit denies for untagging, protected and `cloudguard:` keys). Its trust policy
  (`tagger-trust-policy.json`) must name the server's role; the server uses `cg-proposal-voice-*` session names to
  fit it.
- Missing permissions are reported per resource (for example `FAILED_WRITE, ACCESS_DENIED`), never as success.

## Fixture mode

With `CLOUDGUARD_MODE=fixture` the same flow runs against an in-memory copy of the demo account
(`fixtures/governance-demo.json`, account 123456789012) with a development identity. No AWS call is made, the
fixture allow-list is seeded in `<store>/voice-fixture`, and every answer says "Fixture mode, simulated account".
The MCP tools still read the fixture file, so they don't show tags written by voice.

## Turning it off

- **Immediately, while running:** the simulator's **Disable voice tagging now** button, or create the kill file
  (`New-Item .cloudguard\voice-tagging.disabled` in the server's working directory). Every request is refused at
  once, pending confirmations are cancelled, and a running execution stops before its next write (the remaining
  resources are reported as not attempted). Delete the file to re-enable.
- **Permanently:** unset `CLOUDGUARD_VOICE_TAGGING_ENABLED` and restart.

## Audit

`<store>/voice-tagging-audit.jsonl`, one line per execution or refusal: operator, account reference (last four
digits), execution identity (account masked), service, region, filter, resource names, tags, plan hash, proposal
content hash, confirmation and execution times, and each resource's result. The full proposal, decision and
execution records are in the proposal store as usual (`proposal show` / `history` / `execution-show`). No
credential, token, session id or AWS secret is ever written.

## Known limits

- Not verified against real AWS. The live writer's limits apply (`docs/live-tagging.md`): S3 read-merge-write,
  no conditional tagging APIs, Lambda has no immutable identity.
- Sessions and challenges are in server memory: a restart signs the operator out and voids pending confirmations
  (safe, but you sign in again). One server process only.
- Signing in reloads the simulator page; sign in before connecting the Alexa token.
- The browser's speech recognition decides what text arrives; CloudGuard only accepts exact phrases, so an
  unusual accent may need a second try. Silence never confirms.
- Resource names are read aloud as discovered (untrusted data); they are never interpreted as instructions.
- At most 50 resources per confirmation; supported types are exactly Lambda functions, DynamoDB tables, S3 buckets
  and EC2 instances.
