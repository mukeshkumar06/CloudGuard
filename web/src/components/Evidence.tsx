// Renders the server's structuredContent as-is: scalars as a fact list, arrays of objects as tables, and a bar
// chart only for {label: number} maps or {service, amount} rows actually present in the response. No value is
// computed or invented here beyond the bar widths.

type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

const HIDDEN = new Set(["speech", "schema_version", "tool"]);

function label(key: string): string {
  return key.replace(/_/g, " ");
}

function Scalar({ value }: { value: Json }) {
  if (value === null) return <span className="muted">none</span>;
  if (typeof value === "boolean") return <span className={value ? "ok" : "muted"}>{value ? "yes" : "no"}</span>;
  return <span>{String(value)}</span>;
}

function isScalar(v: Json): boolean {
  return v === null || typeof v !== "object";
}

function Bars({ rows }: { rows: [string, number][] }) {
  const max = Math.max(...rows.map(([, n]) => Math.abs(n)), 0);
  if (!rows.length || max === 0) return null;
  return (
    <div className="bars" role="img" aria-label="bar chart of values from the response">
      {rows.map(([k, n]) => (
        <div className="bar-row" key={k}>
          <span className="bar-label" title={k}>{k}</span>
          <span className="bar-track"><span className="bar-fill" style={{ width: `${(Math.abs(n) / max) * 100}%` }} /></span>
          <span className="bar-value">{n}</span>
        </div>
      ))}
    </div>
  );
}

function numericMap(v: Json): [string, number][] | null {
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  const entries = Object.entries(v);
  if (!entries.length || !entries.every(([, x]) => typeof x === "number")) return null;
  return entries as [string, number][];
}

function amountRows(v: Json): [string, number][] | null {
  if (!Array.isArray(v) || !v.length) return null;
  const rows: [string, number][] = [];
  for (const r of v) {
    if (!r || typeof r !== "object" || Array.isArray(r)) return null;
    const name = (r as Record<string, Json>).service;
    const amount = (r as Record<string, Json>).amount;
    if (typeof name !== "string" || typeof amount !== "string" || Number.isNaN(Number(amount))) return null;
    rows.push([name, Number(amount)]);
  }
  return rows;
}

function Table({ rows }: { rows: Json[] }) {
  const objects = rows.filter((r): r is { [k: string]: Json } => !!r && typeof r === "object" && !Array.isArray(r));
  if (objects.length !== rows.length) {
    return <span>{rows.map((r) => (isScalar(r) ? String(r) : JSON.stringify(r))).join(", ") || <span className="muted">none</span>}</span>;
  }
  const cols = [...new Set(objects.flatMap((o) => Object.keys(o)))];
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{cols.map((c) => <th key={c}>{label(c)}</th>)}</tr></thead>
        <tbody>
          {objects.map((o, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <td key={c}>{c in o ? (isScalar(o[c]) ? <Scalar value={o[c]} /> : <code>{JSON.stringify(o[c])}</code>) : ""}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Section({ name, value, depth }: { name: string; value: Json; depth: number }) {
  const bars = numericMap(value) ?? amountRows(value);
  return (
    <section className="ev-section">
      <h4>{label(name)}</h4>
      {bars && <Bars rows={bars} />}
      {Array.isArray(value) ? <Table rows={value} /> : <Node value={value} depth={depth + 1} />}
    </section>
  );
}

function Node({ value, depth }: { value: Json; depth: number }) {
  if (isScalar(value)) return <Scalar value={value} />;
  if (Array.isArray(value)) return <Table rows={value} />;
  const entries = Object.entries(value as { [k: string]: Json }).filter(([k]) => depth > 0 || !HIDDEN.has(k));
  const scalars = entries.filter(([, v]) => isScalar(v) || (Array.isArray(v) && v.every(isScalar)));
  const nested = entries.filter(([, v]) => !(isScalar(v) || (Array.isArray(v) && v.every(isScalar))));
  return (
    <div>
      {scalars.length > 0 && (
        <dl className="facts">
          {scalars.map(([k, v]) => (
            <div key={k}><dt>{label(k)}</dt><dd>{Array.isArray(v) ? (v.length ? v.join(", ") : <span className="muted">none</span>) : <Scalar value={v} />}</dd></div>
          ))}
        </dl>
      )}
      {nested.map(([k, v]) => <Section key={k} name={k} value={v} depth={depth} />)}
    </div>
  );
}

export function Evidence({ data }: { data: unknown }) {
  if (data === undefined || data === null) return <p className="muted">The server returned no structured content.</p>;
  return <Node value={data as Json} depth={0} />;
}
