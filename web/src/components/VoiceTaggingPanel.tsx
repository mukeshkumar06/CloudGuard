// What personal voice tagging is doing right now: the step, the exact plan the server built (resources, current
// values, changes, overwrites, and why anything is left unchanged), and each resource's outcome. Display only:
// every action goes through the voice controller and the server.
import type { VoiceController } from "../lib/voice";
import { regionLabel } from "../lib/regions";
import { serviceLabel } from "../lib/tagflow";
import { unchangedReason, type WriteFlow } from "../lib/voicewrite";
import { CountTiles, Stepper } from "../ui/kit";
import { useVoiceState } from "./VoicePanel";

const STEP: Record<WriteFlow["stage"], string> = {
  searching: "Searching resources", need_tags: "Waiting for the tag key and value", preparing: "Building the plan",
  confirm: "Waiting for your spoken yes or no", overwrite_ack: "Waiting for “I confirm the overwrite”",
  conflict: "Existing values found: overwrite, change the tag, or cancel", executing: "Applying and verifying",
  done: "Finished", closed: "Ended",
};
const CATEGORY: Record<string, { label: string; tone: string }> = {
  tagged: { label: "Tagged and verified", tone: "ok" }, already_correct: { label: "Already correct", tone: "info" },
  failed: { label: "Failed", tone: "bad" }, skipped: { label: "Skipped", tone: "warn" },
};
const STEPS: WriteFlow["stage"][] = ["searching", "need_tags", "preparing", "confirm", "executing", "done"];

export function VoiceTaggingPanel({ controller }: { controller: VoiceController }) {
  const s = useVoiceState(controller);
  const f = s.writeFlow;
  if (!f) {
    return (
      <section className="card" aria-label="Voice tagging progress">
        <div className="card-head"><div><h2>Voice tagging</h2>
          <p>Say, for example: “Find all Lambda functions containing CodeBridge in Mumbai and tag them.” CloudGuard reads back the exact plan and applies it only after a clear spoken yes.</p></div></div>
      </section>
    );
  }
  const rows = f.plan?.resources ?? [];
  const at = STEPS.indexOf(f.stage === "overwrite_ack" || f.stage === "conflict" ? "confirm" : f.stage);
  return (
    <section className="card" aria-label="Voice tagging progress">
      <div className="card-head">
        <div><h2>Voice tagging</h2>
          <p>{serviceLabel(f.service)} · {regionLabel(f.region)} ({f.region}) · {f.filter.match.replace("_", " ")} “{f.filter.text}”
            {f.tag && <> · <code>{f.tag.key}={f.tag.value}</code></>}{f.overwrite && <> · <span className="pill warn">overwrite</span></>}</p></div>
        <span className={`pill ${f.stage === "done" ? "ok" : f.stage === "closed" ? "muted-pill" : "info"}`}>{STEP[f.stage]}</span>
      </div>
      <Stepper label="Voice tagging steps" at={f.stage === "done" ? STEPS.length : at}
               steps={["Search", "Tag", "Plan", "Confirm", "Apply", "Verified"]} />
      {f.mode === "fixture" && <p className="notice">Fixture mode: the simulated demo account. No AWS call is made and no real tag is written.</p>}
      {f.found && !f.plan && <p className="small">Found {f.found.total} {serviceLabel(f.service, f.found.total)}{f.found.names.length ? `: ${f.found.names.join(", ")}` : ""}.</p>}
      {f.found && !f.found.complete && <p className="notice">Discovery was incomplete: some resources could not be listed, so there may be more matches than shown.</p>}
      {f.stage === "need_tags" && f.partial && (
        <p className="small">Tag so far: key <code>{f.partial.key ?? "?"}</code>, value <code>{f.partial.value ?? "?"}</code>. Waiting for the {f.partial.key ? "value" : "key"}.</p>
      )}
      {f.plan?.resources && !f.results && (() => {
        const rs = f.plan.resources;
        const overwrites = f.plan.overwrites?.length ?? 0;
        return <CountTiles label="Plan counts" items={[
          { label: "Matched", value: rs.length, tone: "info" },
          { label: "To change", value: rs.filter((r) => r.changes.length).length, tone: "ok" },
          { label: "Already correct", value: rs.filter((r) => r.outcome === "ALREADY_MATCHING").length },
          { label: "Unchanged", value: rs.filter((r) => !r.changes.length && r.outcome !== "ALREADY_MATCHING").length },
          { label: "Overwrites", value: overwrites, tone: overwrites ? "warn" : undefined }]} />;
      })()}

      {rows.length > 0 && !f.results && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Resource</th><th>Current value</th><th>Change</th><th>Status</th></tr></thead>
            <tbody>
              {rows.map((r) => {
                const change = r.changes[0];
                return (
                  <tr key={`${r.name}-${r.region}`}>
                    <td>{r.name}<div className="small muted">{r.region}</div></td>
                    <td>{f.tag && r.current_value?.[f.tag.key] != null ? <code>{r.current_value[f.tag.key]}</code>
                      : r.conflicts[0]?.existing_value != null ? <code>{r.conflicts[0].existing_value}</code> : <span className="muted">none</span>}</td>
                    <td>{change ? <>{change.action === "OVERWRITE" ? <span className="pill warn">Overwrite</span> : <span className="pill info">Add</span>}{" "}
                      <code>{change.key}={change.new_value}</code></> : <span className="muted">—</span>}</td>
                    <td>{change ? "Will change after confirmation" : <span className="small">Unchanged: {unchangedReason(r)}</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {f.plan?.overwrites && f.plan.overwrites.length > 0 && !f.results && (
        <p className="notice">This replaces existing values ({f.plan.overwrites.length}). After your yes, CloudGuard asks for a separate “I confirm the overwrite”.</p>
      )}
      {(f.stage === "confirm" || f.stage === "overwrite_ack") && (
        <p className="small muted">Only an exact spoken “yes”, “proceed” or “go ahead” confirms (the server checks the words itself). “No” cancels; a new key or value replaces the plan.</p>
      )}

      {f.results && (
        <>
          <CountTiles label="Execution counts" items={[
            { label: "Tagged and verified", value: f.results.filter((r) => r.category === "tagged" && r.status !== "FAILED_VERIFICATION").length, tone: "ok" },
            { label: "Already correct", value: f.results.filter((r) => r.category === "already_correct").length },
            { label: "Verification failed", value: f.results.filter((r) => r.status === "FAILED_VERIFICATION").length,
              tone: f.results.some((r) => r.status === "FAILED_VERIFICATION") ? "bad" : undefined },
            { label: "Failed or skipped", value: f.results.filter((r) => r.category === "failed" || r.category === "skipped").length,
              tone: f.results.some((r) => r.category === "failed") ? "bad" : undefined }]} />
          <div className="table-wrap">
            <table>
              <thead><tr><th>Resource</th><th>Outcome</th><th>Detail</th></tr></thead>
              <tbody>
                {f.results.map((r) => (
                  <tr key={r.name} className="outcome-row">
                    <td>{r.name}</td>
                    <td>{r.status === "FAILED_VERIFICATION" ? <span className="pill bad">Verification failed</span>
                      : <span className={`pill ${CATEGORY[r.category]?.tone ?? "muted-pill"}`}>{CATEGORY[r.category]?.label ?? r.category}</span>}</td>
                    <td className="small">{r.category === "tagged" ? "Written, then read back and verified"
                      : `${r.status.toLowerCase().replace(/_/g, " ")}${r.error !== "NONE" ? ` · ${r.error.toLowerCase().replace(/_/g, " ")}` : ""}${r.revalidation && r.revalidation !== "ELIGIBLE" ? ` · ${r.revalidation.toLowerCase().replace(/_/g, " ")}` : ""}${r.detail ? ` · ${r.detail}` : ""}`}</td>
                  </tr>
                ))}
                {rows.filter((r) => !r.changes.length && !f.results!.some((x) => x.name === r.name)).map((r) => (
                  <tr key={`u-${r.name}`} className="outcome-row">
                    <td>{r.name}</td><td><span className="pill muted-pill">Not changed</span></td><td className="small">{unchangedReason(r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {f.outcome && <p className="small muted">Execution outcome: {f.outcome}. Success is reported only after read-after-write verification.</p>}
        </>
      )}
    </section>
  );
}
