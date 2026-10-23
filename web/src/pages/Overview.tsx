import { useToolData } from "../ui/data";
import { BarList, Card, CompareBars, DataState, Donut, PageHead, SERVICE_LABEL, STATUS, Stat, money, shortService } from "../ui/kit";
import type { Anomalies, Bill, Breakdown, Comparison, Compliance, Identity, Inventory, PageCtx, Untagged } from "./types";

/** The whole account at a glance, from real tool answers only. Fixture mode loads at once; in live mode the cost
 *  panels wait for a click, because each Cost Explorer request is billed. */
export function Overview({ run, names, mode, go }: PageCtx) {
  const autoCost = mode === "fixture";
  const current = useToolData<Bill>(run, names, "get_current_month_spending", {}, autoCost);
  const last = useToolData<Bill>(run, names, "get_monthly_aws_bill", {}, autoCost);
  const breakdown = useToolData<Breakdown>(run, names, "get_cost_breakdown", {}, autoCost);
  const compare = useToolData<Comparison>(run, names, "compare_monthly_costs", {}, autoCost);
  const anomalies = useToolData<Anomalies>(run, names, "get_cost_anomalies", {}, autoCost);
  const identity = useToolData<Identity>(run, names, "get_aws_account_identity", {}, true);
  const inventory = useToolData<Inventory>(run, names, "get_resource_inventory", { region: "all" }, true);
  const compliance = useToolData<Compliance>(run, names, "scan_tag_compliance", {}, true);
  const untagged = useToolData<Untagged>(run, names, "list_untagged_resources", {}, true);
  const costPanels = [current, last, breakdown, compare, anomalies];
  const loadCost = () => costPanels.forEach((p) => void p.load());
  const refreshAll = () => [...costPanels, identity, inventory, compliance, untagged].forEach((p) => void p.load(true));

  const cur = current.status === "ok" ? current.data : undefined;
  const bill = last.status === "ok" ? last.data : undefined;
  const comp = compliance.status === "ok" ? compliance.data : undefined;
  return (
    <div className="page">
      <PageHead eyebrow="Monitor" title="Overview" intro="Spending, resources and tagging health for the connected account. Every number comes from a CloudGuard tool answer; panels without one say so."
        actions={<>
          {mode !== "fixture" && costPanels.some((p) => p.status === "idle") &&
            <button className="primary" onClick={loadCost}>Load cost data (5 Cost Explorer requests)</button>}
          <button className="secondary" onClick={refreshAll}>Refresh</button>
        </>} />

      <div className="grid stats">
        {identity.status === "ok" && identity.data ? <Stat label="AWS account"
                     value={identity.data.account_id_last4 ? `…${identity.data.account_id_last4}` : "Withheld"}
                     sub={identity.data.error ? `Identity could not be verified (${identity.data.error})`
                       : `${identity.data.synthetic ? "Synthetic fixture account · " : ""}${identity.data.identity_type ?? ""}${identity.data.identity_label ? ` · ${identity.data.identity_label}` : ""}`}
                     tone={identity.data.error ? "warn" : undefined} />
          : <div className="card stat"><span className="label">AWS account</span><DataState {...identity} onLoad={() => void identity.load()} /></div>}
        {cur ? <Stat label="This month so far" value={money(cur.totals?.charges, cur.currency)}
                     sub={<>{cur.period?.label} · {cur.period?.days} days · <span className="pill warn">{cur.warnings.includes("ESTIMATED") ? "Estimated, not a final bill" : "Month to date"}</span></>} />
          : <div className="card stat"><span className="label">This month so far</span><DataState {...current} onLoad={() => void current.load()} cost /></div>}
        {bill ? <Stat label={`Last bill · ${bill.period?.label ?? ""}`} value={money(bill.totals?.calculated_net, bill.currency)}
                      sub={bill.is_final_bill ? "Final bill, net of credits and refunds" : "Not final"}
                      delta={bill.previous_month ? { text: `${Number(bill.previous_month.change) >= 0 ? "▲" : "▼"} ${money(bill.previous_month.change, bill.currency)} charges vs previous month${bill.previous_month.change_percent ? ` (${bill.previous_month.change_percent}%)` : ""}`,
                                                     dir: Number(bill.previous_month.change) > 0 ? "up" : "down" } : undefined} />
          : <div className="card stat"><span className="label">Last month's bill</span><DataState {...last} onLoad={() => void last.load()} cost /></div>}
        {inventory.status === "ok" && inventory.data ? <Stat label="Discovered resources" value={inventory.data.total_resources}
                     sub={`${inventory.data.regions_scanned.length} region(s) · ${inventory.data.coverage_complete ? "complete scan" : "incomplete scan"}`}
                     tone={inventory.data.coverage_complete ? undefined : "warn"} />
          : <div className="card stat"><span className="label">Discovered resources</span><DataState {...inventory} onLoad={() => void inventory.load()} /></div>}
        {comp ? <Stat label="Tag compliance" value={comp.compliance_percentage === null ? "n/a" : `${comp.compliance_percentage}%`}
                      sub={`${comp.non_compliant} non-compliant · ${comp.unknown} unknown`} tone={comp.non_compliant ? "bad" : "ok"} />
          : <div className="card stat"><span className="label">Tag compliance</span><DataState {...compliance} onLoad={() => void compliance.load()} /></div>}
        {untagged.status === "ok" && untagged.data ? <Stat label="Missing mandatory tags" value={untagged.data.total}
                      sub={untagged.data.unknown ? `${untagged.data.unknown} could not be read` : "resources"} tone={untagged.data.total ? "warn" : "ok"} />
          : <div className="card stat"><span className="label">Missing mandatory tags</span><DataState {...untagged} onLoad={() => void untagged.load()} /></div>}
        {anomalies.status === "ok" && anomalies.data ? <Stat label="Cost anomalies" value={anomalies.data.status === "NO_MONITORS" ? "No monitor" : anomalies.data.anomaly_count}
                      sub={anomalies.data.window_start ? `${anomalies.data.window_start} → ${anomalies.data.window_end}` : anomalies.data.status ?? ""}
                      tone={anomalies.data.anomaly_count ? "bad" : "ok"} />
          : <div className="card stat"><span className="label">Cost anomalies</span><DataState {...anomalies} onLoad={() => void anomalies.load()} cost /></div>}
      </div>

      <div className="grid two">
        <Card title="Spend by service" subtitle={breakdown.data?.period ? `${breakdown.data.period.label} · ${breakdown.data.basis ?? ""}` : "Last completed month"}
              actions={<button className="link" onClick={() => go("cost")}>Cost Intelligence →</button>}>
          {breakdown.status === "ok" && breakdown.data ? (
            <BarList labelHeader="Service" valueHeader="Charges" empty="No service charges in this period."
                     items={breakdown.data.services.map((s) => ({ label: shortService(s.service), value: Number(s.amount), display: money(s.amount, breakdown.data!.currency), title: s.service }))} />
          ) : <DataState {...breakdown} onLoad={() => void breakdown.load()} cost />}
        </Card>
        <Card title="Monthly spending" subtitle="Charges before credits, last two completed months"
              actions={<button className="link" onClick={() => go("cost")}>Details →</button>}>
          {compare.status === "ok" && compare.data?.earlier && compare.data.later && compare.data.earlier_charges !== null && compare.data.later_charges !== null ? (
            <>
              <CompareBars earlier={{ label: compare.data.earlier.label, value: Number(compare.data.earlier_charges), display: money(compare.data.earlier_charges, compare.data.currency) }}
                           later={{ label: compare.data.later.label, value: Number(compare.data.later_charges), display: money(compare.data.later_charges, compare.data.currency) }} />
              <p className="small muted">{compare.data.charges_change !== null && <>Change {money(compare.data.charges_change, compare.data.currency)}{compare.data.charges_change_percent ? ` (${compare.data.charges_change_percent}%)` : ""} · </>}{compare.data.verdict?.toLowerCase().replace(/_/g, " ")}</p>
            </>
          ) : <DataState {...compare} onLoad={() => void compare.load()} cost />}
        </Card>
        <Card title="Resources by service" subtitle={inventory.data ? `${inventory.data.regions_scanned.join(", ")}${inventory.data.discovery_error_count ? ` · ${inventory.data.discovery_error_count} discovery error(s)` : ""}` : "Every enabled region"}
              actions={<button className="link" onClick={() => go("governance")}>Governance →</button>}>
          {inventory.status === "ok" && inventory.data ? (
            <BarList labelHeader="Service" valueHeader="Resources" empty="No resources discovered."
                     items={Object.entries(inventory.data.by_service).map(([k, v]) => ({ label: SERVICE_LABEL[k] ?? k, value: v, display: String(v) }))} />
          ) : <DataState {...inventory} onLoad={() => void inventory.load()} />}
        </Card>
        <Card title="Tag compliance" subtitle={comp ? `${comp.total_discovered} resources against the tagging policy` : "Against the active tagging policy"}
              actions={<button className="link" onClick={() => go("tagging")}>Tag Management →</button>}>
          {comp ? (
            <>
              <Donut center={comp.compliance_percentage === null ? "n/a" : `${comp.compliance_percentage}%`} sub="compliant"
                     segments={[{ label: "Compliant", value: comp.compliant, color: STATUS.compliant },
                                { label: "Non-compliant", value: comp.non_compliant, color: STATUS.nonCompliant },
                                { label: "Unknown", value: comp.unknown, color: STATUS.unknown }]} />
              {!comp.coverage_complete && <p className="notice">Incomplete scan: {comp.discovery_error_count} discovery and {comp.evaluation_error_count} evaluation error(s). “Unknown” resources could not be read; they are not counted as compliant.</p>}
            </>
          ) : <DataState {...compliance} onLoad={() => void compliance.load()} />}
        </Card>
      </div>
    </div>
  );
}
