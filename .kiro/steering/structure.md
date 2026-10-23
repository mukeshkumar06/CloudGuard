# Structure

```
backend/cloudguard/
  governance/              resource governance (tag compliance) — first complete module
    models.py              DiscoveredResource, TagFinding, GovernanceScan, ... (pydantic, frozen)
    policy.py              PolicyDocument: defaults + per-type overrides, allowed values, aliases
    policy_store.py        versioned policies: LocalPolicyStore | DynamoPolicyStore, PolicyManager
    discovery/             LiveSource (boto3) | FixtureSource (JSON replay)
      lambda_discovery.py, dynamodb_discovery.py, s3_discovery.py, ec2_discovery.py, common.py
    compliance.py          deterministic evaluate() / summarize() / run_scan()
    custom_tagging/        parser (rules + fallback hook), validation, resolver, engine -> read-only proposals
    report.py              human-readable scan summary
    remediation/           Phase 4: models, pure lifecycle, proposal repository (Local | DynamoDB) +
                           ProposalManager, audit (4.1); identity (STS | dev), versioned authz allow-list,
                           DecisionService approve/reject/revoke + execution re-check (4.2); FIXTURE-ONLY
                           revalidation, FixtureAccount/FixtureWriter, leased executor, hash-chained execution audit (4.3).
                           No live AWS writers.
    __main__.py            CLI: scan | tag | policy | proposal | authz
  cost/                    Task 1 cost intelligence (read-only, deterministic, no LLM): periods, models,
                           ce (client in us-east-1, capped pagination), record_types (explicit RECORD_TYPE table), bill (UTC billing month,
                           UnblendedCost, reconciled categories),
                           anomalies (monitors first, then findings), voice (spoken summaries), CLI.
  governance/recommendations.py  evidence-backed tag recommendations (read-only, deterministic)
  server/                  MCP server (FastMCP): 6 cost + 5 governance + 1 identity read-only tools — never name a package
                           `mcp`, it shadows the SDK
  deploy/                  HTTP deployment prototype (static bearer tokens, alexa:read allowlist, Alexa response
                           projection, Lambda Web Adapter). NOT deployed.
  agents/                  (planned, not implemented) Strands agents over findings
backend/deploy/lambda/     Lambda Web Adapter packaging (Dockerfile, run.sh), prototype only
backend/fixtures/          governance-demo.json, cost fixtures, authz-demo.json (fake account 123456789012)
backend/tests/             pytest, offline (botocore Stubber for discovery)
infra/                     CloudFormation: cloudguard-core.yaml, demo-seed.yaml (not deployed)
web/                       Alexa-compatible MCP simulator (React + Vite + TS, MCP TS SDK client; fixture-verified,
                           Playwright e2e). Browser voice I/O via the Web Speech API. Not an Alexa+ integration; no OAuth.
docs/                      FRICTION_LOG.md (hackathon friction log; update it as friction occurs), product feedback
```
