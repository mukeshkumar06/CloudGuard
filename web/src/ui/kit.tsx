// Reusable presentation pieces. Nothing here calls the server or computes a metric the server didn't return.
import { useState, type ReactNode } from "react";

export function Card({ title, subtitle, actions, children, className = "", label }: {
  title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; label?: string;
}) {
  return (
    <section className={`card ${className}`} aria-label={label}>
      {(title || actions) && (
        <div className="card-head">
          <div>{title && <h2>{title}</h2>}{subtitle && <p>{subtitle}</p>}</div>
          {actions && <div className="page-actions">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function PageHead({ title, intro, actions, eyebrow }: { title: string; intro?: ReactNode; actions?: ReactNode;
                                                               eyebrow?: string }) {
  return (
    <header className="page-head">
      <div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h1>{title}</h1>{intro && <p>{intro}</p>}</div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function Stat({ label, value, sub, delta, tone }: {
  label: string; value: ReactNode; sub?: ReactNode; delta?: { text: string; dir: "up" | "down" | "flat" };
  tone?: "ok" | "warn" | "bad" | "info";
}) {
  return (
    <div className="card stat">
      <span className="label">{label}{tone && <> <span className={`dot ${tone}`} aria-hidden /></>}</span>
      <div className="stat-value">{value}</div>
      {delta && <span className={`stat-sub stat-delta ${delta.dir}`}>{delta.text}</span>}
      {sub && <span className="stat-sub">{sub}</span>}
    </div>
  );
}

export type LoadStatus = "idle" | "loading" | "ok" | "tool-error" | "error" | "unavailable";

/** The four ways a panel can lack data, each said plainly (never shown as a zero). */
export function DataState({ status, error, onLoad, cost }: { status: LoadStatus; error?: string; onLoad?: () => void;
                                                               cost?: boolean }) {
  if (status === "loading") {
    return <div className="state-box" role="status"><div className="skeleton" style={{ width: "60%" }} />
      <div className="skeleton" style={{ width: "40%" }} /><span className="small muted">Asking CloudGuard…</span></div>;
  }
  if (status === "unavailable") {
    return <div className="state-box"><span className="pill muted-pill">Not available</span>
      <span className="small">The server did not list this tool for the connected token.</span></div>;
  }
  if (status === "error" || status === "tool-error") {
    return <div className="state-box error" role="alert"><span className="pill bad">{status === "error" ? "Request failed" : "Tool error"}</span>
      <span className="small">{error ?? "No data was returned."}</span>
      {onLoad && <button className="secondary" onClick={onLoad}>Try again</button>}</div>;
  }
  return <div className="state-box"><span className="small">Not loaded yet.{cost ? " Each Cost Explorer request is billed by AWS (about $0.01)." : ""}</span>
    {onLoad && <button className="secondary" onClick={onLoad}>Load</button>}</div>;
}

export function ModePill({ mode }: { mode?: string }) {
  if (mode === "fixture") return <span className="pill fixture">Fixture data</span>;
  if (mode === "live") return <span className="pill live">Live AWS</span>;
  return <span className="pill bad">Mode unknown</span>;
}

// ---------- charts (single-hue magnitude bars; status colours only for status) ----------

export interface BarItem { label: string; value: number; display: string; title?: string }

function TableToggle({ showTable, onToggle }: { showTable: boolean; onToggle: () => void }) {
  return <button className="link view-toggle" onClick={onToggle} aria-pressed={showTable}>{showTable ? "Chart view" : "Table view"}</button>;
}

/** Horizontal bars for magnitudes (one hue): the length is the value; labels and values are text, not colour. */
export function BarList({ items, valueHeader = "Value", labelHeader = "Name", empty = "Nothing to show." }: {
  items: BarItem[]; valueHeader?: string; labelHeader?: string; empty?: string;
}) {
  const [table, setTable] = useState(false);
  if (!items.length) return <p className="muted small">{empty}</p>;
  const max = Math.max(...items.map((i) => i.value), 0);
  return (
    <div className="bars-wrap">
      <div style={{ display: "flex", justifyContent: "flex-end" }}><TableToggle showTable={table} onToggle={() => setTable(!table)} /></div>
      {table ? (
        <div className="table-wrap"><table><thead><tr><th>{labelHeader}</th><th className="num">{valueHeader}</th></tr></thead>
          <tbody>{items.map((i) => <tr key={i.label}><td>{i.label}</td><td className="num">{i.display}</td></tr>)}</tbody></table></div>
      ) : (
        <div className="bars" role="list">
          {items.map((i) => (
            <div className="bar-row" key={i.label} role="listitem" title={i.title ?? `${i.label}: ${i.display}`}>
              <span className="bar-label">{i.label}</span>
              <span className="bar-track"><span className="bar-fill" style={{ width: max > 0 ? `${(i.value / max) * 100}%` : "0%" }} /></span>
              <span className="bar-value">{i.display}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export interface Segment { label: string; value: number; color: string }

/** Part-to-whole for status counts: a stacked bar with a legend that carries the numbers. */
export function StackBar({ segments }: { segments: Segment[] }) {
  const total = segments.reduce((s, x) => s + x.value, 0);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="stack" role="img" aria-label={segments.map((s) => `${s.label} ${s.value}`).join(", ")}>
        {segments.filter((s) => s.value > 0).map((s) => (
          <span key={s.label} style={{ width: `${(s.value / Math.max(total, 1)) * 100}%`, background: s.color }} title={`${s.label}: ${s.value}`} />
        ))}
      </div>
      <Legend segments={segments} />
    </div>
  );
}

export function Legend({ segments }: { segments: Segment[] }) {
  return <div className="legend">{segments.map((s) => <span key={s.label}><i style={{ background: s.color }} />{s.label} <strong>{s.value}</strong></span>)}</div>;
}

/** A status donut for one headline percentage, with the counts in the legend (never colour alone). */
export function Donut({ segments, center, sub }: { segments: Segment[]; center: string; sub: string }) {
  const total = segments.reduce((s, x) => s + x.value, 0) || 1;
  const r = 52, c = 2 * Math.PI * r;
  let offset = 0;
  return (
    <div className="donut-wrap">
      <svg width="140" height="140" viewBox="0 0 140 140" role="img" aria-label={`${center} ${sub}`}>
        <circle cx="70" cy="70" r={r} fill="none" stroke="rgba(148,163,184,0.10)" strokeWidth="14" />
        {segments.filter((s) => s.value > 0).map((s) => {
          const len = (s.value / total) * c;
          const el = <circle key={s.label} cx="70" cy="70" r={r} fill="none" stroke={s.color} strokeWidth="14"
                             strokeDasharray={`${Math.max(len - 2, 0)} ${c}`} strokeDashoffset={-offset} transform="rotate(-90 70 70)">
            <title>{`${s.label}: ${s.value}`}</title></circle>;
          offset += len;
          return el;
        })}
        <text x="70" y="68" textAnchor="middle" className="donut-center">{center}</text>
        <text x="70" y="86" textAnchor="middle" className="donut-sub">{sub}</text>
      </svg>
      <Legend segments={segments} />
    </div>
  );
}

/** Two periods side by side on one axis. */
export function CompareBars({ earlier, later }: { earlier: { label: string; value: number; display: string };
                                                  later: { label: string; value: number; display: string } }) {
  const max = Math.max(earlier.value, later.value, 0.0001);
  return (
    <div className="compare" role="img" aria-label={`${earlier.label} ${earlier.display}, ${later.label} ${later.display}`}>
      {[earlier, later].map((p, i) => (
        <div className="compare-col" key={p.label} title={`${p.label}: ${p.display}`}>
          <strong className="small">{p.display}</strong>
          <div className={`compare-bar ${i === 0 ? "earlier" : ""}`} style={{ height: `${(p.value / max) * 80}%` }} />
          <span className="small muted">{p.label}</span>
        </div>
      ))}
    </div>
  );
}

export const STATUS = { compliant: "var(--good)", nonCompliant: "var(--critical)", unknown: "#64748b" };

// ---------- icons ----------
const P: Record<string, string> = {
  overview: "M3 13h8V3H3v10Zm0 8h8v-6H3v6Zm10 0h8V11h-8v10Zm0-18v6h8V3h-8Z",
  cost: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm1 15.9V19h-2v-1.1c-1.6-.3-3-1.3-3.1-3h1.9c.1.9.8 1.5 2.2 1.5 1.5 0 1.9-.7 1.9-1.2 0-.6-.4-1.2-2.1-1.6-1.9-.5-3.2-1.2-3.2-2.8 0-1.3 1.1-2.3 2.4-2.6V7h2v1.2c1.4.3 2.4 1.3 2.5 2.7h-1.9c0-.8-.6-1.4-1.6-1.4-1.1 0-1.7.5-1.7 1.1 0 .6.5 1 2.1 1.4 1.7.4 3.2 1.1 3.2 3 0 1.4-1 2.4-2.6 2.7Z",
  governance: "M12 1 3 5v6c0 5.5 3.8 10.7 9 12 5.2-1.3 9-6.5 9-12V5l-9-4Zm-1.2 15.2-3.5-3.5 1.4-1.4 2.1 2.1 4.9-4.9 1.4 1.4-6.3 6.3Z",
  voice: "M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-2.08A7 7 0 0 0 19 12h-2Z",
  tag: "M21.4 11.6 12.4 2.6A2 2 0 0 0 11 2H4a2 2 0 0 0-2 2v7c0 .6.2 1.1.6 1.4l9 9a2 2 0 0 0 2.8 0l7-7a2 2 0 0 0 0-2.8ZM6.5 8A1.5 1.5 0 1 1 6.5 5a1.5 1.5 0 0 1 0 3Z",
  activity: "M13 3a9 9 0 0 0-9 9H1l3.9 3.9L9 12H6a7 7 0 1 1 2.1 5l-1.4 1.4A9 9 0 1 0 13 3Zm-1 5v5l4.3 2.5.7-1.2-3.5-2.1V8H12Z",
  settings: "M19.4 13a7.5 7.5 0 0 0 0-2l2.1-1.6-2-3.5-2.5 1a7.4 7.4 0 0 0-1.7-1L15 3h-4l-.4 2.9a7.4 7.4 0 0 0-1.7 1l-2.5-1-2 3.5L6.6 11a7.5 7.5 0 0 0 0 2l-2.1 1.6 2 3.5 2.5-1a7.4 7.4 0 0 0 1.7 1L11 21h4l.4-2.9a7.4 7.4 0 0 0 1.7-1l2.5 1 2-3.5-2.2-1.6ZM13 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7Z",
  developer: "m8.6 16.6-4.6-4.6 4.6-4.6L7.2 6 1.2 12l6 6 1.4-1.4Zm6.8 0 4.6-4.6-4.6-4.6L16.8 6l6 6-6 6-1.4-1.4Z",
  menu: "M3 6h18v2H3V6Zm0 5h18v2H3v-2Zm0 5h18v2H3v-2Z",
  collapse: "M15.4 7.4 14 6l-6 6 6 6 1.4-1.4L10.8 12l4.6-4.6Z",
  expand: "M8.6 16.6 10 18l6-6-6-6-1.4 1.4 4.6 4.6-4.6 4.6Z",
  shield: "M12 2 4 5v6.1c0 5 3.4 9.7 8 10.9 4.6-1.2 8-5.9 8-10.9V5l-8-3Zm0 2.2 6 2.25v4.65c0 3.9-2.5 7.6-6 8.75-3.5-1.15-6-4.85-6-8.75V6.45l6-2.25Z",
  search: "M15.5 14h-.8l-.3-.3A6.5 6.5 0 1 0 14 15.5l.3.3v.8l5 5 1.5-1.5-5-5Zm-6 0a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9Z",
  check: "M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4L9 16.2Z",
  alert: "M1 21h22L12 2 1 21Zm12-3h-2v-2h2v2Zm0-4h-2v-4h2v4Z",
  lock: "M18 8h-1V6a5 5 0 0 0-10 0v2H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10a2 2 0 0 0-2-2Zm-6 9a2 2 0 1 1 0-4 2 2 0 0 1 0 4Zm3.1-9H8.9V6a3.1 3.1 0 0 1 6.2 0v2Z",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm0 2c-2.7 0-8 1.3-8 4v2h16v-2c0-2.7-5.3-4-8-4Z",
  globe: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm6.9 6h-2.9a15.7 15.7 0 0 0-1.4-3.6A8 8 0 0 1 18.9 8ZM12 4c.8 1.2 1.5 2.5 1.9 4h-3.8c.4-1.4 1.1-2.8 1.9-4ZM4.3 14a8.2 8.2 0 0 1 0-4h3.4a16.5 16.5 0 0 0 0 4H4.3Zm.8 2h2.9c.3 1.3.8 2.5 1.4 3.6A8 8 0 0 1 5.1 16ZM8 8H5.1a8 8 0 0 1 4.3-3.6C8.8 5.5 8.3 6.7 8 8Zm4 12c-.8-1.2-1.5-2.5-1.9-4h3.8c-.4 1.4-1.1 2.8-1.9 4Zm2.3-6H9.7a14.7 14.7 0 0 1 0-4h4.6a14.7 14.7 0 0 1 0 4Zm.3 5.6c.6-1.1 1.1-2.3 1.4-3.6h2.9a8 8 0 0 1-4.3 3.6Zm1.8-5.6a16.5 16.5 0 0 0 0-4h3.4a8.2 8.2 0 0 1 0 4h-3.4Z",
  server: "M4 3h16a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm0 10h16a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1Zm3-7a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3Zm0 10a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3Z",
  power: "M13 3h-2v10h2V3Zm4.8 2.2-1.4 1.4A7 7 0 1 1 7.6 6.6L6.2 5.2A9 9 0 1 0 17.8 5.2Z",
  spark: "M12 2l2.1 6.4L20 10l-5.9 1.6L12 18l-2.1-6.4L4 10l5.9-1.6L12 2Zm7 13 1 3 3 1-3 1-1 3-1-3-3-1 3-1 1-3Z",
};
export type IconName = keyof typeof P;
export function Icon({ name, className = "nav-icon" }: { name: IconName; className?: string }) {
  return <svg className={className} viewBox="0 0 24 24" aria-hidden><path fill="currentColor" d={P[name]} /></svg>;
}

// ---------- counts, steps, sorting ----------

/** Large count tiles for a plan or preview ("Matched 12"). The numbers are the server's; tone is status only. */
export function CountTiles({ items, label }: { items: { label: string; value: ReactNode; tone?: "ok" | "warn" | "bad" | "info" }[];
                                               label?: string }) {
  return (
    <div className="count-tiles" aria-label={label}>
      {items.map((i) => (
        <div key={i.label} className={`count-tile ${i.tone ?? ""}`}>
          <span className="count-value">{i.value}</span><span className="count-label">{i.label}</span>
        </div>
      ))}
    </div>
  );
}

/** A horizontal progress of named steps; `at` is the current one (-1: none yet). */
export function Stepper({ steps, at, label }: { steps: string[]; at: number; label: string }) {
  return (
    <ol className="stepper" aria-label={label}>
      {steps.map((s, i) => (
        <li key={s} className={i < at ? "done" : i === at ? "current" : ""} aria-current={i === at ? "step" : undefined}>
          <span className="step-dot" aria-hidden>{i < at ? "✓" : i + 1}</span><span className="step-label">{s}</span>
        </li>
      ))}
    </ol>
  );
}

export type SortDir = "asc" | "desc";

/** Client-side sorting of rows the server already returned (never re-queried, never re-counted). */
export function useSort<T>(rows: T[], keys: Record<string, (r: T) => string | number>, initial?: string) {
  const [sort, setSort] = useState<{ key?: string; dir: SortDir }>({ key: initial, dir: "asc" });
  const get = sort.key ? keys[sort.key] : undefined;
  const sorted = get ? [...rows].sort((a, b) => {
    const x = get(a), y = get(b);
    const c = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), undefined, { sensitivity: "base", numeric: true });
    return sort.dir === "asc" ? c : -c;
  }) : rows;
  const header = (key: string, text: string, className = "") => (
    <th className={`sortable ${className}`} aria-sort={sort.key === key ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
      <button type="button" className="sort-btn" onClick={() => setSort((s) => ({ key, dir: s.key === key && s.dir === "asc" ? "desc" : "asc" }))}>
        {text}<span className="sort-ind" aria-hidden>{sort.key === key ? (sort.dir === "asc" ? "▲" : "▼") : "↕"}</span>
      </button>
    </th>
  );
  return { sorted, header };
}

// ---------- formatting ----------
export function money(amount: string | number | null | undefined, currency = "USD"): string {
  if (amount === null || amount === undefined || amount === "") return "—";
  const n = Number(amount);
  if (!Number.isFinite(n)) return String(amount);
  return new Intl.NumberFormat("en-US", { style: "currency", currency, maximumFractionDigits: 2 }).format(n);
}

export function shortService(name: string): string {
  return name.replace(/^Amazon |^AWS /, "").replace(" - Compute", " (compute)");
}

export const SERVICE_LABEL: Record<string, string> = { lambda: "Lambda", s3: "S3", dynamodb: "DynamoDB", ec2: "EC2" };
