import { useMemo, useState } from "react";

import { useToolData } from "../ui/data";
import { BarList, Card, CompareBars, DataState, PageHead, Stat, money, shortService } from "../ui/kit";
import type { Anomalies, Bill, Breakdown, Comparison, PageCtx } from "./types";

/** The last six completed months as YYYY-MM, for the month picker (tool arguments only, never data). */
function recentMonths(today = new Date()): { value: string; label: string }[] {
  const out = [{ value: "last-month", label: "Last month" }];
  for (let i = 2; i <= 6; i++) {
    const d = new Date(today.getFullYear(), today.getMonth() - i, 1);
    out.push({ value: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`,
               label: d.toLocaleString("en-US", { month: "long", year: "numeric" }) });
  }
  return out;
}

const TOTAL_ROWS: [keyof NonNullable<Bill["totals"]>, string][] = [
  ["usage", "Usage"], ["fees", "Fees"], ["tax", "Tax"], ["discounts", "Discounts"], ["credits", "Credits"],
  ["refunds", "Refunds"], ["charges", "Charges"], ["calculated_net", "Net (calculated)"]];

function Totals({ b }: { b: Bill }) {
  if (!b.totals) return <p className="muted small">No totals in this answer.</p>;
  return (
    <div className="table-wrap"><table><tbody>
      {TOTAL_ROWS.map(([k, label]) => <tr key={k}><td>{label}</td><td className="num">{money(b.totals![k], b.currency)}</td></tr>)}
    </tbody></table></div>
  );
}

function Drivers({ c }: { c: Comparison }) {
  const rows = [...c.drivers.map((d) => ({ ...d, side: "Driver" })), ...c.offsets.map((d) => ({ ...d, side: "Offset" }))];
  if (!rows.length) return <p className="muted small">No drivers reported.</p>;
  return (
    <div className="table-wrap"><table>
      <thead><tr><th>Change</th><th>Kind</th><th>Name</th><th className="num">Amount</th><th className="num">Share of increase</th></tr></thead>
      <tbody>{rows.map((d) => (
        <tr key={`${d.side}-${d.kind}-${d.name}`}>
          <td><span className={`pill ${d.side === "Driver" ? "warn" : "info"}`}>{d.side}</span></td><td>{d.kind}</td>
          <td>{shortService(d.name)}</td><td className="num">{money(d.change, c.currency)}</td>
          <td className="num">{d.share_of_gross_increase_percent ? `${d.share_of_gross_increase_percent}%` : "—"}</td>
        </tr>))}</tbody>
    </table></div>
  );
}

export function Cost({ run, names, mode }: PageCtx) {
  const auto = mode === "fixture";
  const months = useMemo(() => recentMonths(), []);
  const [month, setMonth] = useState("last-month");
  const monthArgs = month === "last-month" ? {} : { month };
  const bill = useToolData<Bill>(run, names, "get_monthly_aws_bill", monthArgs, auto);
  const current = useToolData<Bill>(run, names, "get_current_month_spending", {}, auto);
  const breakdown = useToolData<Breakdown>(run, names, "get_cost_breakdown", monthArgs, auto);
  const compare = useToolData<Comparison>(run, names, "compare_monthly_costs", {}, auto);
  const investigate = useToolData<Comparison>(run, names, "investigate_cost_increase", {}, false);
  const anomalies = useToolData<Anomalies>(run, names, "get_cost_anomalies", {}, auto);
  const b = bill.data, cur = current.data, br = breakdown.data, cmp = compare.data, inv = investigate.data, an = anomalies.data;

  return (
    <div className="page">
      <PageHead eyebrow="Monitor" title="Cost Intelligence" intro={<>From AWS Cost Explorer through CloudGuard's read-only cost tools. A month in progress is always an estimate, never a final bill.{mode === "live" && " Each request is billed by AWS (about $0.01)."}</>}
        actions={<label className="toggle"><span className="small muted">Month</span>
          <select value={month} onChange={(e) => setMonth(e.target.value)} aria-label="Bill month">
            {months.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select></label>} />

      <div className="grid stats">
        {b && bill.status === "ok" ? <Stat label={`Bill · ${b.period?.label ?? ""}`} value={money(b.totals?.calculated_net, b.currency)}
            sub={b.is_final_bill ? "Final · net of credits and refunds" : "Not final"} />
          : <div className="card stat"><span className="label">Monthly bill</span><DataState {...bill} onLoad={() => void bill.load()} cost /></div>}
        {cur && current.status === "ok" ? <Stat label={`Month to date · ${cur.period?.label ?? ""}`} value={money(cur.totals?.charges, cur.currency)}
            sub={<><span className="pill warn">Estimated</span> {cur.period?.days} days so far, as of {cur.as_of}</>} />
          : <div className="card stat"><span className="label">Month to date</span><DataState {...current} onLoad={() => void current.load()} cost /></div>}
        {cmp && compare.status === "ok" && cmp.charges_change !== null ? <Stat label="Month over month" value={money(cmp.charges_change, cmp.currency)}
            delta={{ text: `${cmp.charges_change_percent ?? "?"}% · ${cmp.earlier?.label} → ${cmp.later?.label}`, dir: Number(cmp.charges_change) > 0 ? "up" : "down" }} />
          : <div className="card stat"><span className="label">Month over month</span><DataState {...compare} onLoad={() => void compare.load()} cost /></div>}
        {an && anomalies.status === "ok" ? <Stat label="Anomalies" value={an.anomaly_count} sub={an.status?.toLowerCase().replace(/_/g, " ")}
            tone={an.anomaly_count ? "bad" : "ok"} />
          : <div className="card stat"><span className="label">Anomalies</span><DataState {...anomalies} onLoad={() => void anomalies.load()} cost /></div>}
      </div>

      <div className="grid two">
        <Card title="Bill breakdown" subtitle={b?.period ? `${b.period.label} · ${b.service_count} services` : undefined} label="Bill breakdown">
          {b && bill.status === "ok" ? <>
            {b.error_code && <p className="notice bad">{b.speech}</p>}
            <Totals b={b} />
            {b.previous_month && <p className="small muted">Charges vs previous month: {money(b.previous_month.change, b.currency)}{b.previous_month.change_percent ? ` (${b.previous_month.change_percent}%)` : ""}</p>}
          </> : <DataState {...bill} onLoad={() => void bill.load()} cost />}
        </Card>
        <Card title="Spend by service" subtitle={br?.basis ?? undefined}>
          {br && breakdown.status === "ok" ? <>
            <BarList labelHeader="Service" valueHeader="Charges" empty="No service charges."
                     items={br.services.map((s) => ({ label: shortService(s.service), value: Number(s.amount), display: money(s.amount, br.currency), title: s.service }))} />
            {br.excluded_tax && <p className="small muted">Tax excluded from the ranking: {money(br.excluded_tax, br.currency)}.</p>}
          </> : <DataState {...breakdown} onLoad={() => void breakdown.load()} cost />}
        </Card>
        <Card title="Month to date" subtitle="Estimated: Cost Explorer finalises a month only after it ends">
          {cur && current.status === "ok" ? <>
            <BarList labelHeader="Service" valueHeader="So far" empty="No charges yet this month."
                     items={cur.top_services.map((s) => ({ label: shortService(s.service), value: Number(s.amount), display: money(s.amount, cur.currency), title: s.service }))} />
            <p className="small muted">Top {cur.top_services.length} of {cur.service_count} services, {cur.period?.start} to {cur.as_of}.</p>
          </> : <DataState {...current} onLoad={() => void current.load()} cost />}
        </Card>
        <Card title="Cost comparison" subtitle={cmp?.verdict ? cmp.verdict.toLowerCase().replace(/_/g, " ") : "Last two completed months"}>
          {cmp && compare.status === "ok" && cmp.earlier && cmp.later && cmp.earlier_charges !== null && cmp.later_charges !== null ? <>
            <CompareBars earlier={{ label: cmp.earlier.label, value: Number(cmp.earlier_charges), display: money(cmp.earlier_charges, cmp.currency) }}
                         later={{ label: cmp.later.label, value: Number(cmp.later_charges), display: money(cmp.later_charges, cmp.currency) }} />
            <Drivers c={cmp} />
          </> : compare.status === "ok" && cmp ? <p className="small">{cmp.speech}</p> : <DataState {...compare} onLoad={() => void compare.load()} cost />}
        </Card>
      </div>

      <Card title="Why did my cost increase?" subtitle="investigate_cost_increase: which services and charge types changed"
            actions={<button className="secondary" onClick={() => void investigate.load()} disabled={investigate.status === "loading" || investigate.status === "unavailable"}>Investigate</button>}>
        {inv && investigate.status === "ok" ? <>
          <p className="answer">{inv.speech}</p>
          <Drivers c={inv} />
          {inv.limitation && <p className="small muted">{inv.limitation}</p>}
        </> : investigate.status === "idle" ? <p className="muted small">Runs on request.</p> : <DataState {...investigate} onLoad={() => void investigate.load()} cost />}
      </Card>

      <Card title="Cost anomalies" subtitle={an?.window_start ? `AWS Cost Anomaly Detection · ${an.window_start} to ${an.window_end}${an.monitor_count !== null ? ` · ${an.monitor_count} monitor(s)` : ""}${an.stale ? " · stale" : ""}` : "AWS Cost Anomaly Detection"}>
        {an && anomalies.status === "ok" ? (an.anomalies.length ? (
          <div className="table-wrap"><table>
            <thead><tr><th>Service</th><th>Region</th><th>Started</th><th className="num">Expected</th><th className="num">Actual</th><th className="num">Impact</th></tr></thead>
            <tbody>{an.anomalies.map((a, i) => (
              <tr key={i}><td>{shortService(a.service)}</td><td>{a.region}</td><td>{a.start_date}{a.end_date ? ` → ${a.end_date}` : " (ongoing)"}</td>
                <td className="num">{money(a.expected_spend, an.currency)}</td><td className="num">{money(a.actual_spend, an.currency)}</td>
                <td className="num"><span className="pill bad">{money(a.impact, an.currency)}{a.impact_percent ? ` · ${a.impact_percent}%` : ""}</span></td></tr>))}</tbody>
          </table></div>
        ) : <p className="small">{an.speech}</p>) : <DataState {...anomalies} onLoad={() => void anomalies.load()} cost />}
      </Card>
    </div>
  );
}
