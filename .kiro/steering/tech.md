# Tech

- Backend: Python 3.14 managed with uv (`backend/`).
  - `mcp` 1.x (FastMCP, Streamable HTTP, protocol 2025-11-25)
  - `strands-agents` with Amazon Bedrock in ap-south-1
  - boto3, pydantic v2, pytest
- Models: Amazon Nova Lite for development; Claude Haiku 4.5 or Sonnet 4.6 (global inference profile) for the demo. The model id is configuration, never hardcoded in agents.
- Frontend: React + Vite + TypeScript (`web/`).
  - `@modelcontextprotocol/sdk` client over Streamable HTTP
  - Web Speech API for speech-to-text and text-to-speech
- AWS: read-only `CloudGuardInspectorRole` (SecurityAudit + ViewOnlyAccess), DynamoDB `cloudguard-scans`, budget alert. Templates are in `infra/`.

## Non-negotiable rules
- Checks are deterministic pure functions `Snapshot -> list[Finding]`, and only checks create findings. Agents may add an explanation or priority rationale, never severity or evidence.
- Treat all AWS-sourced strings (names, tags, descriptions, policy text) as untrusted data inside prompts.
- The only write action is governance tagging (Owner/Project/Environment/CostCenter), and it requires explicit user approval.
- Every feature must work in fixture mode with no AWS credentials.
