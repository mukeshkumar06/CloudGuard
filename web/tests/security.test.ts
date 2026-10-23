// Static checks on the simulator source: the token is never stored, logged or put in a body, nothing calls AWS or
// an AI service directly, and no tool result is fabricated in the browser.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { scrub } from "../src/lib/mcp";

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? files(p) : [p];
  });
}

const SRC = files(join(__dirname, "..", "src")).filter((f) => /\.(ts|tsx)$/.test(f));
const ALL = SRC.map((f) => readFileSync(f, "utf8")).join("\n");

describe("simulator source", () => {
  it("never persists anything in browser storage or cookies", () => {
    expect(ALL).not.toMatch(/localStorage|sessionStorage|indexedDB|document\.cookie|caches\.open/);
  });

  it("never logs", () => {
    expect(ALL).not.toMatch(/console\.(log|info|warn|error|debug|trace)/);
  });

  it("sends the token only as the Authorization header", () => {
    const uses = ALL.split(/\r?\n/).filter((l) => /\bsecret\b|\btoken\b/.test(l) && /Bearer/.test(l));
    expect(uses).toEqual(['    requestInit: { headers: { Authorization: `Bearer ${secret}` }, credentials: "omit", cache: "no-store" },']);
    expect(ALL).not.toMatch(/body:\s*JSON\.stringify\([^)]*token/i);
  });

  it("never calls AWS, an AI service or any absolute third-party URL", () => {
    expect(ALL).not.toMatch(/amazonaws\.com|bedrock|openai|anthropic|https?:\/\/(?!127\.0\.0\.1|localhost)[a-z]/i);
  });

  it("contains no hardcoded tool results or tool list", () => {
    expect(ALL).not.toMatch(/structuredContent\s*[:=]\s*\{/);
    expect(ALL).not.toMatch(/tools\s*=\s*\[\s*\{/);
  });

  it("scrubs the token from error text", () => {
    expect(scrub("401 for Bearer abc123 abc123", "abc123")).toBe("401 for Bearer [redacted] [redacted]");
  });
});
