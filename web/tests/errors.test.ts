import { StreamableHTTPError } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { ErrorCode, McpError } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, it } from "vitest";

import { classify, sanitize, SimulatorError } from "../src/lib/mcp";

const TOKEN = "tok_ABCdef0123456789";
const NASTY = `Error POSTing to endpoint: ${TOKEN} at C:\\Users\\dev\\cloudguard\\x.py /home/dev/app.py `
  + "arn:aws:iam::999988887777:user/bob https://internal.example/debug";

function check(err: unknown, kind: SimulatorError["kind"], text: RegExp) {
  const out = classify(err, TOKEN);
  expect(out.kind).toBe(kind);
  expect(out.message).toMatch(text);
  for (const leak of [TOKEN, "C:\\Users", "/home/dev", "arn:aws", "999988887777", "internal.example", "bob"]) {
    expect(out.message).not.toContain(leak);
  }
}

describe("error classification", () => {
  it("HTTP 401 and 403", () => {
    check(new StreamableHTTPError(401, NASTY), "auth", /401 Unauthorized/);
    check(new StreamableHTTPError(403, NASTY), "auth", /403/);
  });

  it("backend unavailable (proxy 500/502/503/504)", () => {
    for (const s of [500, 502, 503, 504]) check(new StreamableHTTPError(s, NASTY), "unavailable", new RegExp(`HTTP ${s}`));
  });

  it("wrong endpoint", () => {
    check(new StreamableHTTPError(404, NASTY), "http", /No MCP endpoint/);
  });

  it("connection timeout and unexpected disconnect", () => {
    check(new McpError(ErrorCode.RequestTimeout, NASTY), "timeout", /did not answer in time/);
    check(new McpError(ErrorCode.ConnectionClosed, NASTY), "disconnected", /closed/);
  });

  it("malformed structured content and invalid server responses", () => {
    check(new McpError(ErrorCode.InvalidParams, "Structured content does not match the tool's output schema: x"),
      "invalid-response", /did not match/);
    check(new SyntaxError(NASTY), "invalid-response", /not valid MCP JSON/);
    check(new SimulatorError("invalid-response", `no content ${TOKEN}`), "invalid-response", /no content/);
  });

  it("network failure", () => {
    check(new TypeError(NASTY), "network", /could not be reached/);
  });

  it("other MCP errors keep only sanitized server text", () => {
    check(new McpError(ErrorCode.InternalError, NASTY), "mcp", /MCP error -32603/);
    check(new Error(NASTY), "mcp", /unexpected reason/);
  });

  it("sanitize removes tokens, ARNs, account IDs, paths and URLs", () => {
    expect(sanitize(NASTY, TOKEN)).toBe("Error POSTing to endpoint: [redacted] at <path> <path> <arn> <url>");
  });
});
