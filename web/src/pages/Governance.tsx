import { useState } from "react";

import { REGION_OPTIONS } from "../lib/regions";
import { useToolData, type Run } from "../ui/data";
import { BarList, Card, CountTiles, DataState, PageHead, SERVICE_LABEL, STATUS, StackBar, useSort } from "../ui/kit";
import type { Compliance, Explanation, Inventory, Named, PageCtx, Recommendations, Search, Untagged, UntaggedRow } from "./types";

const SERVICES = ["lambda", "s3", "dynamodb", "ec2"];

export function nameOf(r: { display_name: string | null; name_withheld: boolean }): string {
  return r.display_name ?? (r.name_withheld ? "(name withheld)" : "(unnamed)");
}

/** Why one resource is (non-)compliant, and what values the evidence supports. Loads when a row is picked. */
export function FindingDetail({ run, names, findingId }: { run: Run; names: string[]; findingId: string }) {
  const expl = useToolData<Explanation>(run, names, "explain_finding", { finding_id: findingId }, true);
  const recs = useToolData<Recommendations>(run, names, "recommend_tags", { resource: findingId }, true);
  const e = expl.data, r = recs.data;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {e && expl.status === "ok" ? <>
        <div className="card-head"><div><h3>{nameOf({ display_name: e.display_name, name_withheld: !e.display_name })}</h3>
          <p>{SERVICE_LABEL[e.service] ?? e.service} · {e.region} · {e.resource_type}</p></div>
          <span className={`pill ${e.compliance_status === "COMPLIANT" ? "ok" : e.compliance_status === "NON_COMPLIANT" ? "bad" : "muted-pill"}`}>{e.compliance_status.replace(/_/g, " ").toLowerCase()}</span></div>
        <p>{e.explanation}</p>
        <div className="tags">{e.missing_tags.map((k) => <span key={k} className="tag missing">missing {k}</span>)}
          {e.invalid_tags.map((k) => <span key={k} className="tag invalid">invalid {k}</span>)}</div>
        {e.inspection_error && <p className="notice bad">Tags could not be read: {e.inspection_error.kind} ({e.inspection_error.code}). Compliance is unknown, not assumed.</p>}
        <p className="small muted">Evidence: {e.evidence_source_api} at {new Date(e.observed_at).toLocaleString()}</p>
      </> : <DataState {...expl} onLoad={() => void expl.load()} />}
      <h3>Recommended tags</h3>
      {r && recs.status === "ok" ? (
        r.recommendations.length ? (
          <div className="table-wrap"><table>
            <thead><tr><th>Key</th><th>Status</th><th>Recommended value</th><th>Why</th></tr></thead>
            <tbody>{r.recommendations.map((x) => (
              <tr key={x.key}><td><code>{x.key}</code>{x.required && <span className="small muted"> required</span>}</td>
                <td><span className={`pill ${x.status === "RECOMMENDED" ? "ok" : x.status === "CONFLICT" ? "bad" : "warn"}`}>{x.status.replace(/_/g, " ").toLowerCase()}</span></td>
                <td>{x.recommended_value ? <code>{x.recommended_value}</code> : x.value_withheld ? <span className="muted small">withheld (free-form evidence)</span> : <span className="muted">—</span>}</td>
                <td className="small">{x.human_input_reason ?? x.reason_code.replace(/_/g, " ").toLowerCase()} · confidence {x.confidence}</td></tr>))}</tbody>
          </table></div>
        ) : <p className="small">No tag is missing or invalid.</p>
      ) : <DataState {...recs} onLoad={() => void recs.load()} />}
      {r && <p className="small muted">{r.next_step}</p>}
    </div>
  );
}

export function UntaggedTable({ data, selected, onSelect }: { data: Untagged; selected?: string; onSelect: (id: string) => void }) {
  const { sorted, header } = useSort<UntaggedRow>(data.resources, {
    name: (r) => nameOf(r), service: (r) => r.service, region: (r) => r.region, missing: (r) => r.missing_tags.length });
  if (!data.resources.length) return <p className="small">{data.speech}</p>;
  return (
    <div className="table-wrap"><table>
      <thead><tr>{header("name", "Resource")}{header("service", "Service")}{header("region", "Region")}{header("missing", "Missing")}<th>Invalid</th></tr></thead>
      <tbody>{sorted.map((r: UntaggedRow) => (
        <tr key={r.finding_id} className={`clickable ${selected === r.finding_id ? "selected" : ""}`} onClick={() => onSelect(r.finding_id)}
            tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter") onSelect(r.finding_id); }}>
          <td>{nameOf(r)}</td><td>{SERVICE_LABEL[r.service] ?? r.service}</td><td>{r.region}</td>
          <td><div className="tags">{r.missing_tags.map((k) => <span key={k} className="tag missing">{k}</span>)}</div></td>
          <td><div className="tags">{r.invalid_tags.map((k) => <span key={k} className="tag invalid">{k}</span>)}</div></td>
        </tr>))}</tbody>
    </table></div>
  );
}

export function Governance({ run, names }: PageCtx) {
  const [service, setService] = useState("");
  const [regionFilter, setRegionFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [selected, setSelected] = useState<string>();
  const invArgs: Record<string, unknown> = { region: regionFilter, ...(service ? { services: [service] } : {}) };
  const inventory = useToolData<Inventory>(run, names, "get_resource_inventory", invArgs, true);
  const searchArgs: Record<string, unknown> = { text: submitted, match: "contains", ...(service ? { services: [service] } : {}),
                                                ...(regionFilter !== "all" ? { region: regionFilter } : {}) };
  const search = useToolData<Search>(run, names, "search_resources", searchArgs, submitted.length > 0);
  const compliance = useToolData<Compliance>(run, names, "scan_tag_compliance", {}, true);
  const untagged = useToolData<Untagged>(run, names, "list_untagged_resources", service ? { services: [service] } : {}, true);
  const inv = inventory.data, comp = compliance.data;
  const rows: Named[] = submitted && search.status === "ok" && search.data ? search.data.resources : inv?.resources ?? [];
  const [filter, setFilter] = useState("");
  const shown = rows.filter((r) => !filter || nameOf(r).toLowerCase().includes(filter.toLowerCase()));
  const { sorted, header } = useSort<Named>(shown, { name: (r) => nameOf(r), service: (r) => r.service, region: (r) => r.region }, "name");

  return (
    <div className="page">
      <PageHead eyebrow="Monitor" title="Resource Governance" intro="Discovered resources, the tagging policy check, and why each finding exists. Names are the server's safe display names; ARNs and account IDs are never shown." />
      {inv && <CountTiles label="Inventory counts" items={[
        { label: "Resources", value: inv.total_resources, tone: "info" },
        ...Object.entries(inv.by_service).map(([k, v]) => ({ label: SERVICE_LABEL[k] ?? k, value: v })),
        { label: "Regions scanned", value: inv.regions_scanned.length },
        { label: "Coverage", value: inv.coverage_complete ? "Complete" : "Incomplete", tone: inv.coverage_complete ? "ok" as const : "warn" as const }]} />}
      <Card title="Inventory" subtitle={inv ? `${inv.total_resources} resources · ${inv.regions_scanned.length} region(s) scanned${inv.coverage_complete ? "" : " · incomplete"}` : undefined}
            actions={<button className="secondary" onClick={() => void inventory.load(true)}>Refresh scan</button>}>
        <form className="filters" onSubmit={(e) => { e.preventDefault(); setSubmitted(query.trim()); }}>
          <input placeholder="Search names (contains)…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search resource names" />
          <select value={service} onChange={(e) => setService(e.target.value)} aria-label="Service filter">
            <option value="">All services</option>{SERVICES.map((s) => <option key={s} value={s}>{SERVICE_LABEL[s]}</option>)}
          </select>
          <select value={regionFilter} onChange={(e) => setRegionFilter(e.target.value)} aria-label="Region filter">
            <option value="all">All regions</option>{REGION_OPTIONS.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}
          </select>
          <button className="secondary" type="submit">Search</button>
          {submitted && <button type="button" className="link" onClick={() => { setSubmitted(""); setQuery(""); }}>Clear search</button>}
        </form>
        {submitted && search.status !== "ok" ? <DataState {...search} onLoad={() => void search.load()} />
          : !submitted && inventory.status !== "ok" ? <DataState {...inventory} onLoad={() => void inventory.load()} />
          : rows.length ? (<>
            <div className="table-tools">
              <input className="table-filter" placeholder="Filter these rows…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter listed resources" />
              <span className="small muted">{shown.length} of {rows.length} listed{filter ? " (filtered in the browser)" : ""}</span>
            </div>
            <div className="table-wrap"><table>
              <thead><tr>{header("name", "Name")}{header("service", "Service")}{header("region", "Region")}<th /></tr></thead>
              <tbody>{sorted.map((r) => (
                <tr key={r.finding_id} className={`clickable ${selected === r.finding_id ? "selected" : ""}`} onClick={() => setSelected(r.finding_id)}>
                  <td>{nameOf(r)}</td><td>{SERVICE_LABEL[r.service] ?? r.service}</td><td>{r.region}</td>
                  <td><button className="link" onClick={(e) => { e.stopPropagation(); setSelected(r.finding_id); }}>Inspect</button></td></tr>))}</tbody>
            </table></div>
          </>) : <p className="small">{submitted ? search.data?.speech : inv?.speech}</p>}
        {!submitted && inv?.resources_truncated && <p className="small muted">The server lists at most 20 names; counts above are complete. Use search to find others.</p>}
        {submitted && search.data && <p className="small muted">{search.data.total} match(es) · {search.data.coverage_complete ? "complete" : "incomplete coverage"}{search.data.duplicate_count ? ` · ${search.data.duplicate_count} shared name(s)` : ""}</p>}
        {inv && inv.inaccessible_regions.length > 0 && <p className="notice">Not readable: {inv.inaccessible_regions.join(", ")}.</p>}
      </Card>

      <div className="split">
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <Card title="Compliance by service" subtitle={comp ? `${comp.compliance_percentage ?? "n/a"}% compliant overall · ${comp.total_discovered} resources` : undefined}>
            {comp && compliance.status === "ok" ? <>
              <StackBar segments={[{ label: "Compliant", value: comp.compliant, color: STATUS.compliant },
                                   { label: "Non-compliant", value: comp.non_compliant, color: STATUS.nonCompliant },
                                   { label: "Unknown", value: comp.unknown, color: STATUS.unknown }]} />
              <div className="table-wrap"><table>
                <thead><tr><th>Service</th><th className="num">Total</th><th className="num">Compliant</th><th className="num">Non-compliant</th><th className="num">Unknown</th><th className="num">%</th></tr></thead>
                <tbody>{Object.entries(comp.by_service).map(([k, v]) => (
                  <tr key={k}><td>{SERVICE_LABEL[k] ?? k}</td><td className="num">{v.total}</td><td className="num">{v.compliant}</td>
                    <td className="num">{v.non_compliant}</td><td className="num">{v.unknown}</td><td className="num">{v.compliance_percentage ?? "n/a"}</td></tr>))}</tbody>
              </table></div>
              <div className="grid two">
                <div><h3>Missing mandatory tags</h3>
                  <BarList labelHeader="Key" valueHeader="Resources" empty="None missing." items={comp.missing_mandatory_tags.map((m) => ({ label: m.key, value: m.count, display: String(m.count) }))} /></div>
                <div><h3>Invalid values</h3>
                  <BarList labelHeader="Key" valueHeader="Resources" empty="No invalid values." items={comp.invalid_values.map((m) => ({ label: `${m.key} (${m.reason.toLowerCase().replace(/_/g, " ")})`, value: m.count, display: String(m.count) }))} /></div>
              </div>
            </> : <DataState {...compliance} onLoad={() => void compliance.load()} />}
          </Card>
          <Card title="Resources missing mandatory tags" subtitle="Select one to see why and what to add">
            {untagged.status === "ok" && untagged.data ? <>
              <UntaggedTable data={untagged.data} selected={selected} onSelect={setSelected} />
              {untagged.data.unknown > 0 && <p className="small muted">{untagged.data.unknown} resource(s) couldn't be read and aren't listed.</p>}
            </> : <DataState {...untagged} onLoad={() => void untagged.load()} />}
          </Card>
        </div>
        <Card title="Finding detail" label="Finding detail">
          {selected ? <FindingDetail run={run} names={names} findingId={selected} /> : <p className="muted small">Select a resource to see why it is (non-)compliant and the tag values the evidence supports.</p>}
        </Card>
      </div>
    </div>
  );
}
