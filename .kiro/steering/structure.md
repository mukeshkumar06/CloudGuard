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
    __main__.py            CLI: scan | tag | policy
  server/                  (planned) MCP server — never name a package `mcp`, it shadows the SDK
  agents/                  (planned) Strands agents over findings
backend/fixtures/          governance-demo.json (replay data, fake account 123456789012)
backend/tests/             pytest, offline (botocore Stubber for discovery)
infra/                     CloudFormation: cloudguard-core.yaml, demo-seed.yaml
web/                       (planned) React voice client (simulated Alexa+)
docs/                      friction log, product feedback
```
