// A thin wrapper over the official MCP TypeScript SDK client. The SDK performs the real protocol work:
// `initialize` (offering its latest protocol version and accepting the one the server answers with),
// the `notifications/initialized` notification, the `MCP-Protocol-Version` header on every later request,
// `Mcp-Session-Id` propagation when a server issues one (CloudGuard's server is stateless and issues none),
// and JSON or event-stream responses. Nothing here executes a tool or produces a result locally.
//
// Token handling: the bearer token lives only in this module's closure while connected. It is sent only as the
// Authorization header, never in a request body, never logged, never stored, and scrubbed from error text.

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport, StreamableHTTPError } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { ErrorCode, McpError, type CallToolResult, type Tool } from "@modelcontextprotocol/sdk/types.js";

export type ErrorKind = "timeout" | "unavailable" | "auth" | "http" | "mcp" | "invalid-response" | "disconnected"
  | "network" | "input";

export class SimulatorError extends Error {
  constructor(public kind: ErrorKind, message: string, public status?: number) {
    super(message);
  }
}

export const CONNECT_TIMEOUT_MS = 15_000;
export const CALL_TIMEOUT_MS = 60_000;

export interface Connection {
  serverName: string;
  serverVersion: string;
  protocolVersion: string | undefined;
  sessionId: string | undefined; // undefined for CloudGuard's stateless server
  instructions: string | undefined;
  tools: Tool[];
  callTool(name: string, args: Record<string, unknown>): Promise<CallToolResult>;
  close(): Promise<void>;
}

export function scrub(text: string, secret: string): string {
  return secret ? text.split(secret).join("[redacted]") : text;
}

/** Remove what must never reach the screen: the token, ARNs, account IDs, file paths, URLs. */
export function sanitize(text: string, secret: string): string {
  return scrub(text, secret)
    .replace(/arn:aws[\w-]*:[^\s"'`,;()<>]*/g, "<arn>")
    .replace(/(?<!\d)\d{12}(?!\d)/g, "<account>")
    .replace(/\b[A-Za-z]:[\\/][^\s"'`]*/g, "<path>")
    .replace(/(?<![\w:/.-])\/(?:[^\s/"'`]+\/)+[^\s"'`]*/g, "<path>")
    .replace(/https?:\/\/\S+/g, "<url>")
    .slice(0, 300);
}

/** A fixed, understandable message per failure. Server text is shown only for MCP errors, and only sanitized. */
export function classify(err: unknown, secret: string): SimulatorError {
  if (err instanceof SimulatorError) return new SimulatorError(err.kind, sanitize(err.message, secret), err.status);
  if (err instanceof StreamableHTTPError) {
    const status = err.code;
    if (status === 401) return new SimulatorError("auth", "The server rejected the token (401 Unauthorized). Check you pasted the Alexa token.", 401);
    if (status === 403) return new SimulatorError("auth", "Access denied (403): the token has no CloudGuard scope, or the request origin is not allowed.", 403);
    if (status === 404 || status === 405) return new SimulatorError("http", `No MCP endpoint at this URL (HTTP ${status}). Use /mcp.`, status);
    if (status !== undefined && status >= 500) {
      return new SimulatorError("unavailable", `The CloudGuard server is unavailable or failed (HTTP ${status}). Check that the backend is running, then reconnect.`, status);
    }
    return new SimulatorError("http", `The server answered with HTTP ${status ?? "error"}.`, status);
  }
  if (err instanceof McpError) {
    if (err.code === ErrorCode.RequestTimeout) return new SimulatorError("timeout", "The server did not answer in time. Try again; if it persists, check the backend.");
    if (err.code === ErrorCode.ConnectionClosed) return new SimulatorError("disconnected", "The connection to the server was closed. Reconnect to continue.");
    if (/structured content|output schema/i.test(err.message)) {
      return new SimulatorError("invalid-response", "The server's structured response did not match the tool's advertised schema, so it was not shown.");
    }
    const text = err.message.replace(/^MCP error -?\d+:\s*/, ""); // McpError already prefixes its code
    return new SimulatorError("mcp", `MCP error ${err.code}: ${sanitize(text, secret)}`);
  }
  if (err instanceof SyntaxError) return new SimulatorError("invalid-response", "The server sent a response that is not valid MCP JSON.");
  if (err instanceof TypeError) {
    return new SimulatorError("network", "The server could not be reached. Check the URL and that the backend and dev proxy are running.");
  }
  return new SimulatorError("mcp", "The request failed for an unexpected reason.");
}

export function resolveUrl(url: string): URL {
  const parsed = new URL(url.trim(), window.location.href);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new SimulatorError("input", "The server URL must use http or https.");
  }
  const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(parsed.hostname);
  if (parsed.protocol === "http:" && !loopback) {
    throw new SimulatorError("input", "Refusing to send a bearer token over plain http to a non-loopback host.");
  }
  return parsed;
}

export async function connect(url: string, token: string): Promise<Connection> {
  const secret = token.trim();
  if (!secret) throw new SimulatorError("input", "Enter the Alexa bearer token.");
  if (/\s/.test(secret)) throw new SimulatorError("input", "The token must not contain spaces.");
  const endpoint = resolveUrl(url);
  const transport = new StreamableHTTPClientTransport(endpoint, {
    requestInit: { headers: { Authorization: `Bearer ${secret}` }, credentials: "omit", cache: "no-store" },
  });
  const client = new Client({ name: "cloudguard-mcp-simulator", version: "0.1.0" }, { capabilities: {} });
  try {
    await client.connect(transport, { timeout: CONNECT_TIMEOUT_MS }); // initialize + notifications/initialized
    const tools: Tool[] = [];
    let cursor: string | undefined;
    do {
      const page = await client.listTools(cursor ? { cursor } : undefined, { timeout: CONNECT_TIMEOUT_MS });
      tools.push(...page.tools);
      cursor = page.nextCursor;
    } while (cursor);
    const info = client.getServerVersion();
    return {
      serverName: info?.name ?? "unknown",
      serverVersion: info?.version ?? "unknown",
      protocolVersion: transport.protocolVersion,
      sessionId: transport.sessionId,
      instructions: client.getInstructions(),
      tools,
      async callTool(name, args) {
        try {
          const result = (await client.callTool({ name, arguments: args }, undefined, { timeout: CALL_TIMEOUT_MS })) as CallToolResult;
          if (!Array.isArray(result?.content)) throw new SimulatorError("invalid-response", "The server's tool result had no content list.");
          if (!result.isError && (typeof result.structuredContent !== "object" || result.structuredContent === null)) {
            throw new SimulatorError("invalid-response", "The tool returned no structured content, so there is nothing to show.");
          }
          return result;
        } catch (err) {
          throw classify(err, secret);
        }
      },
      async close() {
        await client.close().catch(() => undefined);
      },
    };
  } catch (err) {
    await client.close().catch(() => undefined);
    throw classify(err, secret);
  }
}

export async function serverMode(url: string): Promise<string | undefined> {
  // GET /healthz (no auth, no account data) next to the MCP endpoint: tells fixture from live.
  try {
    const health = new URL("/healthz", resolveUrl(url));
    const r = await fetch(health, { credentials: "omit", cache: "no-store", signal: AbortSignal.timeout(5_000) });
    const body = (await r.json()) as { mode?: unknown };
    return typeof body.mode === "string" ? body.mode : undefined;
  } catch {
    return undefined;
  }
}
