# CloudGuard AI

**The cloud security engineer your startup can't afford yet — that you can just ask.**

CloudGuard AI is a voice-first, agentic AWS governance assistant. Ask it about your AWS account and it does three things:
- Inspects the account's real configuration through deterministic, read-only checks.
- Explains the evidence-backed findings through Amazon Bedrock agents built with Strands.
- Proposes fixes that change nothing until you approve them.

It is exposed as a self-hosted **MCP server** (spec 2025-11-25, Streamable HTTP) and demoed through a simulated Alexa+ web client.

> Status: in development for the Amazon Developer Hackathon (Alexa+ track + AWS Builder mini challenge).

## Repository layout
- `backend/` — Python MCP server, checks, agents (uv project)
- `infra/` — CloudFormation: `cloudguard-core.yaml` (role, table, budget) and `demo-seed.yaml` (deliberate misconfigurations)
- `web/` — React voice client (coming)
- `docs/` — friction log, product feedback

## Quick start (backend)
```bash
cd backend
uv sync
uv run pytest
```

## License
MIT. See [LICENSE](LICENSE).
