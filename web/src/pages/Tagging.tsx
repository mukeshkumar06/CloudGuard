import { useState, type ReactNode } from "react";

import { VoiceTaggingPanel } from "../components/VoiceTaggingPanel";
import { REGION_OPTIONS } from "../lib/regions";
import type { VoiceController } from "../lib/voice";
import { useToolData } from "../ui/data";
import { Card, CountTiles, DataState, PageHead, SERVICE_LABEL, type LoadStatus } from "../ui/kit";
import { FindingDetail, UntaggedTable, nameOf } from "./Governance";
import type { PageCtx, Preview, Untagged } from "./types";

export interface TaggingRecord { at: Date; service: string; region: string; tag: string; outcome?: string;
                                 results: { name: string; category: string; status: string; error: string }[] }

/** Preview a change with the read-only dry run. It never changes AWS; applying needs the voice flow (personal
 *  accounts) or the CLI approval workflow. */
function PreviewBuilder({ run, names, region }: PageCtx) {
  const [form, setForm] = useState({ service: "lambda", match: "contains", text: "", key: "", value: "", region, overwrite: false });
  const [state, setState] = useState<{ status: LoadStatus; data?: Preview; error?: string }>({ status: "idle" });
  const set = (k: string, v: string | boolean) => setForm((f) => ({ ...f, [k]: v }));
  async function preview(e: React.FormEvent) {
    e.preventDefault();
    if (!form.text || !form.key) return;
    setState({ status: "loading" });
    const out = await run("preview_tag_changes", { tags: { [form.key]: form.value }, service: form.service,
      ...(form.match === "exact" ? { resource_name: form.text } : { name_match: form.match, name_text: form.text }),
      region: form.region, ...(form.overwrite ? { overwrite: true } : {}) }, "bypass");
    if (out.error) setState({ status: "error", error: out.error.message });
    else if (out.result?.isError) setState({ status: "tool-error", error: (out.result.content?.[0] as { text?: string } | undefined)?.text });
    else setState({ status: "ok", data: out.result?.structuredContent as unknown as Preview });
  }
  const p = state.data;
  return (
    <Card title="Preview a tag change" subtitle="preview_tag_changes: a dry run. Nothing is written and nothing is stored.">
      {!names.includes("preview_tag_changes") ? <DataState status="unavailable" /> : <>
        <form className="filters" onSubmit={preview}>
          <select value={form.service} onChange={(e) => set("service", e.target.value)} aria-label="Preview service">
            {Object.entries(SERVICE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
          <select value={form.match} onChange={(e) => set("match", e.target.value)} aria-label="Preview name match">
            <option value="contains">name contains</option><option value="starts_with">starts with</option>
            <option value="ends_with">ends with</option><option value="exact">exact name</option></select>
          <input style={{ width: 170 }} placeholder="codebridge" value={form.text} onChange={(e) => set("text", e.target.value)} aria-label="Preview name text" />
          <select value={form.region} onChange={(e) => set("region", e.target.value)} aria-label="Preview region">
            {REGION_OPTIONS.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}</select>
          <input style={{ width: 130 }} placeholder="key" value={form.key} onChange={(e) => set("key", e.target.value)} aria-label="Preview tag key" />
          <input style={{ width: 150 }} placeholder="value" value={form.value} onChange={(e) => set("value", e.target.value)} aria-label="Preview tag value" />
          <label className="toggle small"><input type="checkbox" checked={form.overwrite} onChange={(e) => set("overwrite", e.target.checked)} /> overwrite one tag</label>
          <button className="primary" type="submit" disabled={!form.text || !form.key || state.status === "loading"}>Preview</button>
        </form>
        {state.status === "ok" && p ? <>
          <p className="notice info">Preview only. AWS has not changed. {p.speech}</p>
          <p className="small"><span className="label">Status</span> <span className="pill info">{p.status.replace(/_/g, " ").toLowerCase()}</span></p>
          <CountTiles label="Preview counts" items={[
            { label: "Matched", value: p.matched, tone: "info" }, { label: "To change", value: p.changing, tone: "ok" },
            { label: "Unchanged", value: Math.max(p.matched - p.changing, 0) },
            { label: "Conflicts", value: p.conflicts, tone: p.conflicts ? "warn" : undefined },
            { label: "Overwrites", value: p.overwrite_count ?? 0, tone: p.overwrite_count ? "warn" : undefined }]} />
          {(p.overwrite_count ?? 0) > 0 && <p className="notice"><strong>Overwrite warning:</strong> {p.overwrite_count} existing value(s) would be replaced. The voice flow asks for a separate “I confirm the overwrite”.</p>}
          {p.resources.length > 0 && <div className="table-wrap"><table><thead><tr><th>Resource</th><th>Service</th><th>Region</th></tr></thead>
            <tbody>{p.resources.map((r, i) => <tr key={i}><td>{nameOf(r)}</td><td>{SERVICE_LABEL[r.service] ?? r.service}</td><td>{r.region}</td></tr>)}</tbody></table></div>}
          {p.conflicts > 0 && <p className="small muted">A conflict is an existing different value: adding a tag never overwrites it. Current values aren't sent to this client; the operator voice flow shows them.</p>}
          {p.resources_truncated && <p className="small muted">The list is truncated; counts are complete.</p>}
          <p className="small muted">{p.next_step}</p>
        </> : state.status !== "idle" && <DataState status={state.status} error={state.error} />}
      </>}
    </Card>
  );
}

export function Tagging(props: PageCtx & { voice: VoiceController; operator: ReactNode; history: TaggingRecord[] }) {
  const { run, names, voice, operator, history } = props;
  const untagged = useToolData<Untagged>(run, names, "list_untagged_resources", {}, true);
  const [selected, setSelected] = useState<string>();
  return (
    <div className="page">
      <PageHead eyebrow="Act" title="Tag Management" intro="What is missing, what the evidence recommends, what a change would do (preview), and what voice tagging actually applied. Tags are only ever added or updated, never removed." />
      <div className="split">
        <div style={{ display: "flex", flexDirection: "column", gap: 20, minWidth: 0 }}>
          <VoiceTaggingPanel controller={voice} />
          <PreviewBuilder {...props} />
          <Card title="Resources missing mandatory tags" subtitle="Select one for its recommended values">
            {untagged.status === "ok" && untagged.data ? <UntaggedTable data={untagged.data} selected={selected} onSelect={setSelected} />
              : <DataState {...untagged} onLoad={() => void untagged.load()} />}
          </Card>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 20, minWidth: 0 }}>
          {operator}
          <Card title="Recommendations" label="Tag recommendations">
            {selected ? <FindingDetail run={run} names={names} findingId={selected} /> : <p className="muted small">Select a resource on the left.</p>}
          </Card>
          <Card title="Applied this session" subtitle="Per-resource outcomes of voice tagging executions in this browser session">
            {history.length ? history.map((h, i) => (
              <div key={i} className="result">
                <div className="result-head"><strong>{SERVICE_LABEL[h.service] ?? h.service} · {h.region}</strong><code>{h.tag}</code>
                  {h.outcome && <span className={`pill ${h.outcome === "SUCCEEDED" ? "ok" : h.outcome === "PARTIAL" ? "warn" : "bad"}`}>{h.outcome.toLowerCase()}</span>}
                  <span className="small muted">{h.at.toLocaleTimeString()}</span></div>
                <div className="tags">{h.results.map((r) => <span key={r.name} className={`tag ${r.category === "failed" ? "missing" : r.category === "skipped" ? "invalid" : ""}`}>{r.name}: {r.category.replace("_", " ")}</span>)}</div>
              </div>)) : <p className="muted small">Nothing applied in this session.</p>}
          </Card>
        </div>
      </div>
    </div>
  );
}
