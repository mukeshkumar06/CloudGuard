// The connection screen. The token lives only in App's state until connect() runs, then only inside the
// connection's closure: this component never stores, logs or displays it.
import { useState, type ReactNode } from "react";

import type { SimulatorError } from "../lib/mcp";
import { Icon, type IconName } from "../ui/kit";
import { Logo, type ConnStatus } from "./Shell";

const CAPABILITIES: { icon: IconName; title: string; text: string }[] = [
  { icon: "spark", title: "Agentic cloud investigation", text: "Ask about spend, resources and tagging in plain language, by voice or text." },
  { icon: "governance", title: "Evidence-backed answers", text: "Deterministic checks decide every finding; each one carries the raw AWS evidence." },
  { icon: "lock", title: "Human-approved actions", text: "Read-only by default. Tag writes need an operator session and a spoken confirmation." },
];

/** "/mcp" (same-origin proxy) or an absolute http(s) URL. */
export function urlProblem(url: string): string | undefined {
  const u = url.trim();
  if (!u) return "Enter the MCP server URL.";
  if (u.startsWith("/")) return undefined;
  try {
    const p = new URL(u);
    return p.protocol === "http:" || p.protocol === "https:" ? undefined : "Use an http:// or https:// URL, or /mcp.";
  } catch {
    return "That isn't a valid URL. Use /mcp for the local proxy, or a full https:// URL.";
  }
}

function FlowDiagram() {
  const nodes = [["Browser", "MCP TS client"], ["/mcp", "same-origin proxy"], ["CloudGuard", "MCP server"], ["AWS", "read-only APIs"]];
  return (
    <svg className="flow" viewBox="0 0 400 92" role="img" aria-label="Connection flow: browser MCP client, same-origin proxy, CloudGuard MCP server, AWS read-only APIs">
      <defs>
        <linearGradient id="flowLine" x1="0" x2="1"><stop offset="0" stopColor="var(--accent)" /><stop offset="1" stopColor="var(--accent-2)" /></linearGradient>
      </defs>
      {nodes.map(([a, b], i) => {
        const x = 6 + i * 100;
        return (
          <g key={a}>
            {i > 0 && <line x1={x - 14} y1="38" x2={x - 2} y2="38" stroke="url(#flowLine)" strokeWidth="2" strokeDasharray="3 3" />}
            <rect x={x} y="14" width="82" height="48" rx="10" className="flow-node" />
            <text x={x + 41} y="35" textAnchor="middle" className="flow-title">{a}</text>
            <text x={x + 41} y="51" textAnchor="middle" className="flow-sub">{b}</text>
          </g>
        );
      })}
      <text x="200" y="84" textAnchor="middle" className="flow-sub">Streamable HTTP · Authorization: Bearer (memory only)</text>
    </svg>
  );
}

interface Props {
  url: string;
  setUrl: (u: string) => void;
  token: string;
  setToken: (t: string) => void;
  status: ConnStatus;
  error: SimulatorError | null;
  onConnect: (e: React.FormEvent) => void;
  operator?: ReactNode;
}

export function ConnectScreen({ url, setUrl, token, setToken, status, error, onConnect, operator }: Props) {
  const [touched, setTouched] = useState(false);
  const urlError = urlProblem(url);
  const tokenError = token.trim() ? undefined : "Paste the Alexa bearer token (alexa:read).";
  const connecting = status === "connecting";
  return (
    <div className="connect-layout">
      <aside className="connect-hero" aria-label="About CloudGuard">
        <div className="hero-brand"><Logo /><div><strong>CloudGuard AI</strong><span className="muted small">AWS governance assistant</span></div></div>
        <p className="hero-lede">The cloud security engineer your startup can't afford yet, that you can just ask.</p>
        <ul className="capabilities">
          {CAPABILITIES.map((c) => (
            <li key={c.title}><span className="cap-icon"><Icon name={c.icon} className="row-icon" /></span>
              <div><strong>{c.title}</strong><p className="small muted">{c.text}</p></div></li>
          ))}
        </ul>
        <div className={`hero-status ${status}`} role="status">
          <span className={`dot ${status === "connected" ? "ok" : status === "connecting" ? "warn" : ""}`} aria-hidden />
          {status === "connecting" ? "Connecting to the MCP server…" : status === "connected" ? "Connected" : "Not connected"}
        </div>
      </aside>

      <section className="connect-card card" aria-label="Connect">
        <span className="eyebrow">MCP connection</span>
        <h1>Connect your AWS governance assistant</h1>
        <p className="muted">This page is a real MCP client. It performs the handshake (<code>initialize</code>, <code>notifications/initialized</code>, <code>tools/list</code>) over Streamable HTTP, then shows only what the server returns.</p>
        <FlowDiagram />
        <form onSubmit={(e) => { setTouched(true); if (urlError || tokenError) { e.preventDefault(); return; } onConnect(e); }}
              className="connect-form" noValidate>
          <label className="field">
            <span>MCP server URL</span>
            <input value={url} onChange={(e) => setUrl(e.target.value)} autoComplete="off" spellCheck={false}
                   aria-invalid={touched && !!urlError} aria-describedby="url-help" disabled={connecting} />
            <small id="url-help" className={touched && urlError ? "field-error" : "muted"}>
              {touched && urlError ? urlError : <>Default <code>/mcp</code> goes through the same-origin dev proxy to the local server.</>}</small>
          </label>
          <label className="field">
            <span>Alexa bearer token</span>
            <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" spellCheck={false}
                   aria-invalid={touched && !!tokenError} aria-describedby="token-help" disabled={connecting} />
            <small id="token-help" className={touched && tokenError ? "field-error" : "muted"}>
              {touched && tokenError ? tokenError : "Kept in memory only and sent only as the Authorization header. Never stored or shown."}</small>
          </label>
          <button className="primary connect-btn" type="submit" disabled={connecting}>
            {connecting ? <><span className="spinner-ring" aria-hidden /> Connecting…</> : "Connect"}
          </button>
        </form>
        {error && <div className="error-box" role="alert"><strong>Connection failed</strong><p>{error.message}</p></div>}
        <p className="small muted security-note"><Icon name="lock" className="row-icon" /> The token is cleared from this form as soon as you connect. It is never written to browser storage or cookies.</p>
      </section>
      {operator && <div className="connect-operator">{operator}</div>}
    </div>
  );
}
