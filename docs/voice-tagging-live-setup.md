# Enabling voice-only tagging on the live server (personal account)

A step-by-step setup for one person on their own development account: say the request, confirm by voice, and the
server tags, with no CLI approval step. The rules are in `docs/voice-tagging.md`.

> **Not yet verified against real AWS.** Start with one throwaway tag on one resource (step 7). Every step below
> is run by you. Steps 1–3 change IAM in your account and step 6 restarts your server.

Run everything in **PowerShell** from the repository root unless a step says `backend/`. It uses your default
CLI profile, the one the live server already runs with.

## 0. Look up your identity

```powershell
$Account = aws sts get-caller-identity --query Account --output text
$UserArn = aws sts get-caller-identity --query Arn --output text      # arn:aws:iam::<acct>:user/<name>
$UserId  = aws sts get-caller-identity --query UserId --output text   # AIDA...
$Account; $UserArn; $UserId
```

`$UserArn` must be an IAM **user** (`:user/`). If it shows `assumed-role`, stop and ask: the bindings in step 3
would be different.

## 1. The tagging role (the only identity that writes tags)

It is built from the reviewed templates in `infra/iam/proposed/`. Only your user may assume it, and only with a
`cg-proposal-*` session name. It can add or update tags on Lambda, DynamoDB, EC2 and S3, and read them back. It can
never remove tags, never write `cloudguard:` keys, and never touch resources tagged `cloudguard:protected=true`.

```powershell
(Get-Content infra\iam\proposed\tagger-trust-policy.json -Raw) `
  -replace 'arn:aws:iam::<ACCOUNT_ID>:role/<CLOUDGUARD_BACKEND_ROLE>', $UserArn |
  Set-Content "$env:TEMP\cg-tagger-trust.json" -Encoding ascii

(Get-Content infra\iam\proposed\tagger-role-policy.json -Raw) `
  -replace ', "<PROTECTED_TAG_KEYS>"', '' -replace '<ACCOUNT_ID>', $Account |
  Set-Content "$env:TEMP\cg-tagger-policy.json" -Encoding ascii

aws iam create-role --role-name CloudGuardVoiceTagger --assume-role-policy-document "file://$env:TEMP\cg-tagger-trust.json"
aws iam put-role-policy --role-name CloudGuardVoiceTagger --policy-name CloudGuardVoiceTagging --policy-document "file://$env:TEMP\cg-tagger-policy.json"
$TaggerArn = "arn:aws:iam::${Account}:role/CloudGuardVoiceTagger"
```

The trust policy names your user directly, so your user needs no extra `sts:AssumeRole` permission. Both are in
the same account.

## 2. An allow-list administrator role

The allow-list rules don't let one person hold both `admin` and `approve`. Your user gets
create/approve/reject/execute, and a separate role administers the allow-list. That role needs no permissions:
it only has to prove its identity with STS.

```powershell
@"
{ "Version": "2012-10-17", "Statement": [ { "Effect": "Allow", "Principal": { "AWS": "$UserArn" }, "Action": "sts:AssumeRole" } ] }
"@ | Set-Content "$env:TEMP\cg-admin-trust.json" -Encoding ascii

aws iam create-role --role-name CloudGuardAuthzAdmin --assume-role-policy-document "file://$env:TEMP\cg-admin-trust.json"
$AdminArn = "arn:aws:iam::${Account}:role/CloudGuardAuthzAdmin"

aws configure set role_arn $AdminArn --profile cg-authz-admin
aws configure set source_profile default --profile cg-authz-admin
aws configure set region ap-south-1 --profile cg-authz-admin
aws sts get-caller-identity --profile cg-authz-admin   # should show assumed-role/CloudGuardAuthzAdmin/...
```

IAM changes can take a few seconds to apply. If the last command is denied, wait and retry.

## 3. The live allow-list

```powershell
@"
{
  "schema_version": "cloudguard.authz/v1",
  "accounts": {
    "$Account": {
      "admin":   ["$AdminArn"],
      "create":  ["$UserArn"],
      "approve": ["$UserArn"],
      "reject":  ["$UserArn"],
      "execute": ["$UserArn"],
      "bindings": [
        { "kind": "iam_user", "principal": "$UserArn", "person": "mukesh", "unique_id": "$UserId" },
        { "kind": "single_person_role", "principal": "$AdminArn", "person": "authz-admin" }
      ]
    }
  }
}
"@ | Set-Content "$env:TEMP\cg-authz-live.json" -Encoding ascii

cd backend
$env:CLOUDGUARD_AUTHZ_BOOTSTRAP_ADMIN = $AdminArn
.venv\Scripts\python.exe -m cloudguard.governance authz save "$env:TEMP\cg-authz-live.json" --expected-version 0 --mode live --profile cg-authz-admin
Remove-Item Env:CLOUDGUARD_AUTHZ_BOOTSTRAP_ADMIN
cd ..
```

This writes `backend\.cloudguard\authz`, which the server reads when it starts from `backend/`. The voice path
never edits it.

## 4. A dedicated operator credential

```powershell
cd backend
.venv\Scripts\python.exe -m cloudguard.deploy --new-token
cd ..
```

Keep the printed **token** somewhere private (a password manager). It is what you paste into "Sign in as operator".
You need only the printed **SHA-256** in step 6. Don't reuse the Alexa or MCP operator tokens.

## 5. Stop the running live server

Stop the backend process on port 8080 (Ctrl+C in its terminal). Leave Vite (5173) running.

## 6. Restart it with voice tagging on

In a new PowerShell from `backend/`, set the same variables you use today (mode, both MCP token digests,
`AWS_PROFILE`), then add the voice-tagging ones:

```powershell
$env:CLOUDGUARD_MODE = "live"
$env:CLOUDGUARD_TOKEN_SHA256 = "<your operator MCP token sha256, as today>"
$env:CLOUDGUARD_ALEXA_TOKEN_SHA256 = "<your Alexa token sha256, as today>"

$env:CLOUDGUARD_VOICE_TAGGING_ENABLED = "true"
$env:CLOUDGUARD_VOICE_OPERATOR_TOKEN_SHA256 = "<sha256 from step 4>"
$env:CLOUDGUARD_VOICE_TAGGING_ACCOUNTS = "<your 12-digit account id>"
$env:CLOUDGUARD_VOICE_TAGGING_ROLE_ARN = "arn:aws:iam::<your account id>:role/CloudGuardVoiceTagger"

.venv\Scripts\python.exe -m cloudguard.deploy --port 8080
```

Check it from another terminal:

```powershell
curl.exe -s http://127.0.0.1:5173/operator/session
```

You should see `"enabled": true, "mode": "live"`, not `voice_tagging_disabled`.

## 7. First test: one resource, one harmless tag

1. Open http://127.0.0.1:5173 (Edge gives the best voices). Click **Sign in as operator** and paste the token from
   step 4. The page reloads.
2. Connect with the Alexa token. Tag Management should show *Personal voice tagging: Ready*.
3. In **Voice Assistant**, name one function exactly: *"Tag the Lambda function named exactly codebridge-api in
   Mumbai."* (use a real name). Say **yes** to "I heard…", then *"cg-voice-test is yes"*, check the plan it
   reads out, and say **yes**.
4. Expect *Tagged and verified 1*. Check in the console or with
   `aws lambda list-tags --resource <function ARN>`.

Then try the bulk request: *"Tag all the Lambdas which have CodeBridge."*

## Turning it off

- **At once:** **Disable voice tagging now** in the app, or `New-Item backend\.cloudguard\voice-tagging.disabled`.
  Delete the file to re-enable.
- **Permanently:** restart without `CLOUDGUARD_VOICE_TAGGING_ENABLED`.
- **Remove the IAM roles:**

```powershell
aws iam delete-role-policy --role-name CloudGuardVoiceTagger --policy-name CloudGuardVoiceTagging
aws iam delete-role --role-name CloudGuardVoiceTagger
aws iam delete-role --role-name CloudGuardAuthzAdmin
```

## If something fails

| What you see | Likely cause |
|---|---|
| `voice_tagging_disabled` | `CLOUDGUARD_VOICE_TAGGING_ENABLED` isn't `true` in the server's terminal |
| The server won't start: "needs CLOUDGUARD_VOICE_TAGGING_ROLE_ARN" | Step 6's role ARN is missing or in another account |
| "That account isn't allowed for voice tagging" | `CLOUDGUARD_VOICE_TAGGING_ACCOUNTS` doesn't match `$Account` |
| `NOT_AUTHORIZED` / person resolution error | The allow-list (step 3) wasn't saved, or the server runs as a different identity than `$UserArn` |
| `FAILED_WRITE, ACCESS_DENIED` per resource | The tagging role can't be assumed yet (IAM delay), or the resource type isn't covered |
| The audit trail | `backend\.cloudguard\voice-tagging-audit.jsonl` |
