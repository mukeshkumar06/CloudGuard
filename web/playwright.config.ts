// End-to-end tests against a real fixture-mode CloudGuard server and the real Vite dev proxy. Nothing is mocked.
// Ports differ from the manual demo (8080 / 5173) so a running demo is never reused by accident.
import { createHash, randomBytes } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { defineConfig } from "@playwright/test";

const BACKEND_PORT = 8091;
const WEB_PORT = 5174;
const sha = (t: string) => createHash("sha256").update(t).digest("hex");

// Random throwaway tokens, created once in the runner process; workers inherit them through the environment.
process.env.CG_E2E_ALEXA_TOKEN ??= randomBytes(24).toString("base64url");
process.env.CG_E2E_OPERATOR_TOKEN ??= randomBytes(24).toString("base64url");
process.env.CG_E2E_VOICE_TOKEN ??= randomBytes(24).toString("base64url");
process.env.CG_E2E_VOICE_STORE ??= join(tmpdir(), `cloudguard-e2e-voice-${process.pid}`);
process.env.CG_E2E_AWS_LOG ??= join(tmpdir(), `cloudguard-e2e-aws-calls-${process.pid}.log`);
process.env.COMSPEC ??= "C:\\Windows\\System32\\cmd.exe";

const python = process.platform === "win32" ? ".venv\\Scripts\\python.exe" : ".venv/bin/python";

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    viewport: { width: 1366, height: 768 },
    trace: "off",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: `${python} ../web/e2e/fixture_backend.py`,
      cwd: "../backend",
      url: `http://127.0.0.1:${BACKEND_PORT}/healthz`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        CG_E2E_BACKEND_PORT: String(BACKEND_PORT),
        CG_E2E_AWS_LOG: process.env.CG_E2E_AWS_LOG,
        CLOUDGUARD_TOKEN_SHA256: sha(process.env.CG_E2E_OPERATOR_TOKEN),
        CLOUDGUARD_ALEXA_TOKEN_SHA256: sha(process.env.CG_E2E_ALEXA_TOKEN),
        // Personal voice tagging against the simulated fixture account only (no AWS), with a throwaway store.
        CLOUDGUARD_VOICE_TAGGING_ENABLED: "true",
        CLOUDGUARD_VOICE_OPERATOR_TOKEN_SHA256: sha(process.env.CG_E2E_VOICE_TOKEN),
        CLOUDGUARD_VOICE_TAGGING_ACCOUNTS: "123456789012",
        CLOUDGUARD_VOICE_TAGGING_STORE: process.env.CG_E2E_VOICE_STORE,
        CLOUDGUARD_VOICE_TAGGING_KILL_FILE: join(process.env.CG_E2E_VOICE_STORE, "voice-tagging.disabled"),
      },
    },
    {
      command: `npx vite --port ${WEB_PORT} --strictPort`,
      url: `http://127.0.0.1:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { CLOUDGUARD_MCP_TARGET: `http://127.0.0.1:${BACKEND_PORT}` },
    },
  ],
});
