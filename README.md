# CloudGuard AI

**The cloud security engineer your startup can't afford yet — that you can just ask.**

CloudGuard AI is an AWS governance assistant designed for voice-first use. Ask it about your AWS account and it:
- Inspects the account's real configuration through deterministic, read-only checks (cost, tag governance).
- Explains each finding with its raw AWS evidence and a short spoken summary. These are deterministic templates; Amazon Bedrock / Strands agents are planned but not implemented.
- Recommends tag values only from evidence, and changes nothing. Approved tagging runs only against a simulated fixture account; there is no live AWS writer.

It is exposed as a self-hosted **MCP server** (spec 2025-11-25, Streamable HTTP). It is demoed through an **Alexa-compatible MCP simulator**: a web app that is a real MCP client and uses CloudGuard's Alexa-scoped token. This is **not** an Alexa+ integration. There is no Amazon account linking, and no Alexa device or service is involved.

> Status: in development for the Amazon Developer Hackathon (Alexa+ track + AWS Builder mini challenge).

## Verification status
| Capability | Fixture verified | Live AWS verified | Deployed | Production ready |
|---|---|---|---|---|
| 12 read-only MCP tools (6 cost, 5 governance, 1 identity) | Yes, all 12 | Discovery partially (Lambda, DynamoDB, S3 read once). Cost Explorer: a live run of the cost CLI was reported on 2026-10-16, but the raw output was not retained. The MCP server and the identity tool were **not** run live | No | No |
| Alexa-scoped surface: `alexa:read` token, 12-tool allowlist, response projection | Yes | No | No | No |
| Alexa-compatible MCP simulator (`web/`), including browser voice input and output (Web Speech API) | Yes. A real MCP client; Playwright tests run against the real fixture backend, with the browser speech APIs replaced by test fakes | No | No | No |
| HTTP deployment prototype (Lambda Web Adapter) | Yes, locally | No | **No: nothing is deployed** | No |

**Not implemented:** OAuth / Alexa+ account linking, Bedrock and Strands agents, and live AWS tag writes. Authentication uses static bearer tokens in the HTTP deployment prototype (`python -m cloudguard.deploy`); the local MCP server (`python -m cloudguard.server`) has no authentication and binds to `127.0.0.1` only.

## Repository layout
- `backend/` — Python MCP server (cost and governance tools), deployment prototype, checks (uv project)
- `infra/` — CloudFormation: `cloudguard-core.yaml` (role, table, budget) and `demo-seed.yaml` (deliberate misconfigurations), neither deployed
- `web/` — Alexa-compatible MCP simulator (React + Vite + TypeScript); see [web/README.md](web/README.md)
- `docs/` — friction log, product feedback

## Quick start (backend)
```bash
cd backend
uv sync
uv run pytest
```

## Quick start (simulator, fixture mode)
See [web/README.md](web/README.md) for the token setup, the fixture-mode backend command (`python -m cloudguard.deploy`), `npm run dev`, and the demo walkthrough.

## License
MIT. See [LICENSE](LICENSE).
