import type { ReactNode } from "react";

import type { Connection } from "../lib/mcp";
import type { OperatorSession } from "../lib/operator";
import { REGION_OPTIONS } from "../lib/regions";
import { Card, ModePill, PageHead } from "../ui/kit";

interface Props {
  conn: Connection;
  url: string;
  mode?: string;
  names: string[];
  region: string;
  setRegion: (r: string) => void;
  session: OperatorSession | null;
  operator: ReactNode;
  onClearCache: () => void;
  onDisconnect: () => void;
}

/** Only what the simulator actually supports: the connection, the region default, the answer cache, and the
 *  server-side state of voice tagging. Nothing here is stored in the browser. */
export function Settings({ conn, url, mode, names, region, setRegion, session, operator, onClearCache, onDisconnect }: Props) {
  const has = (t: string) => names.includes(t);
  return (
    <div className="page">
      <PageHead eyebrow="Account" title="Settings" intro="Connection, region and safety status for this browser session. Settings last until the page reloads; nothing is saved in the browser." />
      <div className="grid two">
        <Card title="Connection" subtitle="The MCP server this page talks to" actions={<button className="secondary" onClick={onDisconnect}>Disconnect</button>}>
          <dl className="kv">
            <dt>Server URL</dt><dd><code>{url}</code></dd>
            <dt>Server</dt><dd>{conn.serverName} {conn.serverVersion}</dd>
            <dt>Protocol</dt><dd>{conn.protocolVersion ?? "not reported"}</dd>
            <dt>Session</dt><dd>{conn.sessionId ? `${conn.sessionId.slice(0, 8)}…` : "stateless (no session id)"}</dd>
            <dt>Caller</dt><dd>Alexa token (alexa:read), held in memory only</dd>
            <dt>Data mode</dt><dd><ModePill mode={mode} /></dd>
            <dt>Tools listed</dt><dd>{names.length}</dd>
          </dl>
        </Card>
        <Card title="Region preference" subtitle="Offered first when CloudGuard asks which region to search">
          <label className="field">
            <span className="small muted">Preferred region</span>
            <select value={region} onChange={(e) => setRegion(e.target.value)} aria-label="Default region">
              {REGION_OPTIONS.map((r) => <option key={r.code} value={r.code}>{r.label} ({r.code})</option>)}
            </select>
          </label>
          <p className="small muted" style={{ marginTop: 10 }}>A resource listing that names no region always asks “Which AWS region should I search?” first; this preference is never used in its place. Cost answers are account-wide.</p>
        </Card>
        <Card title="Answer cache" subtitle="Repeated questions reuse a recent answer instead of calling the server">
          <p className="small muted">Memory only, scoped to this connection, cleared on disconnect. Clearing it makes the next question call the server (and, in live mode, AWS) again.</p>
          <div className="plan-actions"><button className="secondary" onClick={onClearCache}>Clear answer cache</button></div>
        </Card>
        <Card title="Tagging availability" subtitle="What this server allows">
          <dl className="kv">
            <dt>Preview (dry run)</dt><dd>{has("preview_tag_changes") ? <span className="pill ok">Available</span> : <span className="pill muted-pill">Not listed</span>}</dd>
            <dt>Recommendations</dt><dd>{has("recommend_tags") ? <span className="pill ok">Available</span> : <span className="pill muted-pill">Not listed</span>}</dd>
            <dt>Voice tagging</dt><dd>{!session ? <span className="pill muted-pill">Not enabled on the server</span>
              : session.kill_switch ? <span className="pill bad">Kill switch engaged</span>
              : session.authenticated ? <span className="pill ok">Operator signed in</span> : <span className="pill warn">Operator signed out</span>}</dd>
            <dt>Typed chat</dt><dd>Preview only; never applies tags</dd>
          </dl>
          <p className="small muted" style={{ marginTop: 10 }}>There is no MCP write tool. Tags are applied only by the operator voice flow after a spoken confirmation, or through the CLI approval workflow.</p>
        </Card>
      </div>
      {operator}
    </div>
  );
}
