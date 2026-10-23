import type { Tool } from "@modelcontextprotocol/sdk/types.js";

import { ResultView, type Execution } from "../components/ResultView";
import { ToolExplorer } from "../components/ToolExplorer";
import { group, readOnly, title } from "../lib/tools";
import { Card, PageHead } from "../ui/kit";

interface Props {
  tools: Tool[];
  selected: string | null;
  preset?: Record<string, unknown>;
  busy: boolean;
  findings: { finding_id: string; label: string }[];
  execs: Execution[];
  open: (name: string, args: Record<string, unknown>) => void;
  call: (name: string, args: Record<string, unknown>) => Promise<unknown>;
}

/** Every tool the server listed, its schema, direct calls and the raw results: for developers, not the main UI. */
export function Developer({ tools, selected, preset, busy, findings, execs, open, call }: Props) {
  const groups: Record<string, Tool[]> = {};
  for (const t of tools) (groups[group(t)] ??= []).push(t);
  const tool = tools.find((t) => t.name === selected) ?? null;
  return (
    <div className="page">
      <PageHead eyebrow="Developer" title="MCP Explorer" intro="Dynamic tools/list discovery: The MCP tools the server returned for this token, with their schemas. Calls made here bypass the answer cache." />
      <div className="dev-layout">
        <Card>
          <nav aria-label="Discovered tools">
            <p className="muted small">{tools.length} tools returned by <code>tools/list</code></p>
            {Object.entries(groups).map(([g, ts]) => (
              <div key={g} className="tool-group">
                <h3>{g}</h3>
                {ts.map((t) => (
                  <button key={t.name} className={`tool-item ${selected === t.name ? "active" : ""}`} onClick={() => open(t.name, {})} title={t.name}>
                    <span>{title(t)}</span>
                    {readOnly(t) === "read-only" && <span className="ro" title="readOnlyHint from the server">RO</span>}
                  </button>
                ))}
              </div>
            ))}
          </nav>
        </Card>
        <section className="tool-pane" aria-label="Tool explorer and results">
          {tool ? <Card><ToolExplorer tool={tool} preset={preset} busy={busy} findings={findings} onRun={(args) => call(tool.name, args) as never} /></Card>
            : <div className="empty">Select a tool to see its schema and call it directly.</div>}
          <h2 className="history-title">Results</h2>
          {execs.length === 0 ? <div className="empty">No tool has been called yet.</div> : execs.map((x) => <ResultView key={x.id} ex={x} />)}
        </section>
      </div>
    </div>
  );
}
