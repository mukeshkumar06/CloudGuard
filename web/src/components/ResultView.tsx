import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import type { SimulatorError } from "../lib/mcp";
import { Evidence } from "./Evidence";

export interface Execution {
  id: number;
  tool: string;
  args: Record<string, unknown>;
  state: "running" | "ok" | "tool-error" | "failed";
  startedAt: Date;
  ms?: number;
  result?: CallToolResult;
  error?: SimulatorError;
  reusedAt?: Date; // this answer was later reused from the conversation cache
}

function textOf(result: CallToolResult): string {
  return result.content.filter((c) => c.type === "text").map((c) => (c as { text: string }).text).join("\n");
}

const ERROR_TITLE: Record<SimulatorError["kind"], string> = {
  timeout: "Timed out",
  unavailable: "Server unavailable",
  auth: "Not authorized",
  http: "HTTP error",
  mcp: "MCP error",
  "invalid-response": "Invalid server response",
  disconnected: "Disconnected",
  network: "Network error",
  input: "Invalid input",
};

const STATE_LABEL: Record<Execution["state"], string> = {
  running: "Running…",
  ok: "Succeeded",
  "tool-error": "Tool error (isError)",
  failed: "Request failed",
};

export function ResultView({ ex }: { ex: Execution }) {
  const structured = ex.result?.structuredContent as Record<string, unknown> | undefined;
  const speech = typeof structured?.speech === "string" ? structured.speech : undefined;
  const outcome = typeof structured?.outcome === "string" ? structured.outcome : undefined;
  const tone = ex.state === "ok" ? (outcome === "findings" ? "warn" : outcome === "unavailable" ? "warn" : "ok")
    : ex.state === "running" ? "info" : "bad";
  return (
    <article className="result" aria-busy={ex.state === "running"}>
      <header className="result-head">
        <code className="tool-name">{ex.tool}</code>
        <span className={`pill ${tone}`}>{STATE_LABEL[ex.state]}{outcome ? ` · ${outcome}` : ""}</span>
        {ex.ms !== undefined && <span className="muted small">{ex.ms} ms</span>}
        {ex.reusedAt && <span className="pill info" title={`Last reused ${ex.reusedAt.toLocaleTimeString()}`}>Cached · reused</span>}
        {structured?.mode === "fixture" && <span className="pill fixture">FIXTURE DATA</span>}
        {structured?.mode === "live" && <span className="pill live">LIVE AWS DATA</span>}
      </header>
      <details className="args"><summary>Arguments sent</summary><pre>{JSON.stringify(ex.args, null, 2)}</pre></details>

      {ex.state === "running" && <div className="spinner" role="status">Calling the CloudGuard MCP server…</div>}

      {ex.state === "failed" && ex.error && (
        <div className="error-box" role="alert">
          <strong>{ERROR_TITLE[ex.error.kind]}</strong>
          <p>{ex.error.message}</p>
          <p className="muted small">No result is shown because the server did not return one.</p>
        </div>
      )}

      {ex.state === "tool-error" && ex.result && (
        <div className="error-box" role="alert">
          <strong>The tool returned an error</strong>
          <p>{textOf(ex.result)}</p>
        </div>
      )}

      {ex.state === "ok" && ex.result && (
        <div className="result-body">
          <section className="speech" aria-label="What a voice assistant would say">
            <h3>Voice response <span className="muted small">(the tool's <code>speech</code> field)</span></h3>
            {speech ? <p className="speech-text">“{speech}”</p> : <p className="muted">This result has no speech field.</p>}
          </section>
          <section className="evidence" aria-label="Structured evidence">
            <h3>Structured evidence <span className="muted small">(<code>structuredContent</code> returned to the MCP client)</span></h3>
            <Evidence data={ex.result.structuredContent} />
          </section>
        </div>
      )}

      {ex.result && (
        <details className="raw">
          <summary>Raw MCP result (developer)</summary>
          <pre>{JSON.stringify(ex.result, null, 2)}</pre>
        </details>
      )}
    </article>
  );
}
