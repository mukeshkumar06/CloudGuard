import { useState } from "react";

import { ResultView, type Execution } from "../components/ResultView";
import { Card, CountTiles, PageHead, SERVICE_LABEL } from "../ui/kit";
import type { VoiceTurn } from "./Assistant";
import type { TaggingRecord } from "./Tagging";

const TONE: Record<Execution["state"], string> = { ok: "ok", running: "warn", "tool-error": "bad", failed: "bad" };

/** This browser session's activity only. The server keeps the durable audit (proposal store and the voice tagging
 *  audit log), which no API exposes to the browser; read it with the CLI. */
export function Activity({ execs, history, voiceLog = [] }: { execs: Execution[]; history: TaggingRecord[]; voiceLog?: VoiceTurn[] }) {
  const [open, setOpen] = useState<number>();
  const failed = execs.filter((x) => x.state === "failed" || x.state === "tool-error").length;
  return (
    <div className="page">
      <PageHead eyebrow="Account" title="Activity & Audit" intro="Every tool call, spoken exchange and voice tagging execution in this browser session, newest first." />
      <div className="audit-split">
        <div className="audit-source session"><span className="pill info">Browser session</span>
          <p className="small">Shown below. Kept in this tab's memory only; gone on reload or disconnect. Not an audit record.</p></div>
        <div className="audit-source durable"><span className="pill ok">Durable backend audit</span>
          <p className="small">Stays on the server; no API exposes it to the browser, so nothing here reads or invents it. Read it with <code>python -m cloudguard.governance proposal history ID</code>, <code>proposal execution-show ID EXEC_ID</code>, and the voice tagging audit log in the server's store.</p></div>
      </div>
      <CountTiles label="Session counts" items={[{ label: "Tool calls", value: execs.length },
        { label: "Failed calls", value: failed, tone: failed ? "bad" : undefined },
        { label: "Spoken replies", value: voiceLog.length }, { label: "Voice tagging runs", value: history.length }]} />
      <div className="split">
        <Card title="Tool calls" subtitle={`${execs.length} in this session`}>
          {execs.length === 0 ? <div className="empty">No tool has been called yet.</div> : (
            <div className="timeline">
              {execs.map((x) => (
                <div key={x.id} className="timeline-item">
                  <span className={`dot ${TONE[x.state]}`} style={{ marginTop: 6 }} aria-hidden />
                  <div style={{ minWidth: 0 }}>
                    <button className="link" style={{ padding: 0 }} onClick={() => setOpen(open === x.id ? undefined : x.id)}><code>{x.tool}</code></button>
                    <div className="small muted">{JSON.stringify(x.args)}{x.reusedAt ? " · reused from cache" : ""}</div>
                    {open === x.id && <div style={{ marginTop: 8 }}><ResultView ex={x} /></div>}
                  </div>
                  <div className="small muted" style={{ textAlign: "right" }}>{x.startedAt.toLocaleTimeString()}{x.ms !== undefined && <div>{x.ms} ms</div>}</div>
                </div>
              ))}
            </div>
          )}
        </Card>
        <div style={{ display: "flex", flexDirection: "column", gap: 20, minWidth: 0 }}>
        <Card title="Voice tagging executions" subtitle="Per-resource outcomes reported by the server">
          {history.length === 0 ? <div className="empty">No voice tagging in this session.</div> : history.map((h, i) => (
            <div key={i} className="result">
              <div className="result-head"><strong>{SERVICE_LABEL[h.service] ?? h.service} · {h.region}</strong><code>{h.tag}</code>
                <span className="small muted">{h.at.toLocaleTimeString()}</span>{h.outcome && <span className="pill info">{h.outcome.toLowerCase()}</span>}</div>
              <div className="table-wrap"><table><tbody>{h.results.map((r) => (
                <tr key={r.name}><td>{r.name}</td><td>{r.category.replace("_", " ")}</td><td className="small muted">{r.status.toLowerCase().replace(/_/g, " ")}{r.error !== "NONE" ? ` · ${r.error.toLowerCase()}` : ""}</td></tr>))}</tbody></table></div>
            </div>))}
        </Card>
        <Card title="Spoken exchanges" subtitle="What CloudGuard said, in order">
          {voiceLog.length === 0 ? <div className="empty">No spoken exchange yet.</div> : (
            <ol className="history">{[...voiceLog].reverse().slice(0, 15).map((t) => (
              <li key={t.id}>{t.you && <p className="turn you"><span className="label">You</span>“{t.you}”</p>}
                <p className="turn cg"><span className="label">CloudGuard</span>{t.reply}</p>
                <span className="small muted">{t.at.toLocaleTimeString()}</span></li>))}</ol>
          )}
        </Card>
        </div>
      </div>
    </div>
  );
}
