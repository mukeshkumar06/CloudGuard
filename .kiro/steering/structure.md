# Structure

```
backend/cloudguard/
  governance/              resource governance (tag compliance) — first complete module
    models.py              DiscoveredResource, TagFinding, GovernanceScan, ... (pydantic, frozen)
    policy.py              TagPolicy (default: Environment, Project, Owner, CostCenter)
    discovery/             LiveSource (boto3) | FixtureSource (JSON replay)
      lambda_discovery.py, dynamodb_discovery.py, s3_discovery.py, ec2_discovery.py, common.py
    compliance.py          deterministic evaluate() / summarize() / run_scan()
    __main__.py            CLI: python -m cloudguard.governance scan --mode fixture|live
  server/                  (planned) MCP server — never name a package `mcp`, it shadows the SDK
  agents/                  (planned) Strands agents over findings
backend/fixtures/          governance-demo.json (replay data, fake account 123456789012)
backend/tests/             pytest, offline (botocore Stubber for discovery)
infra/                     CloudFormation: cloudguard-core.yaml, demo-seed.yaml
web/                       (planned) React voice client (simulated Alexa+)
docs/                      friction log, product feedback
```
