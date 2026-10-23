// The typed conversation: messages, plans, confirmations and inline results. Moved out of App.tsx unchanged in
// behaviour; App still owns every state change and every server call.
import type { Plan } from "../lib/intent";
import { handoffCommands, type TagFlow } from "../lib/tagflow";
import { ResultView, type Execution } from "./ResultView";

export type Msg = { id: number; role: "user" | "assistant"; text: string; plan?: Plan; execId?: number;
                    cached?: { age: string }; answer?: string; options?: string[]; flow?: TagFlow };

export const EXAMPLES = [
  "Which AWS account am I using?",
  "What was my AWS bill last month?",
  "How much have I spent this month?",
  "Which services are costing me the most?",
  "Compare my AWS costs with last month.",
  "Why did my AWS cost increase?",
  "Are there any cost anomalies?",
  "How many AWS resources do I have?",
  "Check my tagging compliance.",
  "Show me untagged resources.",
  "Explain this finding.",
  "Recommend tags for this resource.",
  "Find all Lambdas with codebridge in their names and tag them.",
];

interface Props {
  msgs: Msg[];
  execs: Execution[];
  busy: boolean;
  question: string;
  setQuestion: (q: string) => void;
  submit: (q: string) => void;
  confirm: (m: Msg, mode?: "use" | "refresh") => void;
  confirmFlow: (version: number) => void;
  currentFlow: TagFlow | null;
  openInExplorer: (tool: string, args: Record<string, unknown>) => void;
  regionChoices: { code: string; label: string }[]; // offered for "Which AWS region should I search?"
}

export function Conversation({ msgs, execs, busy, question, setQuestion, submit, confirm, confirmFlow, currentFlow,
                               openInExplorer, regionChoices }: Props) {
  return (
    <section className="conversation card" aria-label="Conversation">
      <div className="card-head">
        <div><h2>Ask CloudGuard</h2>
          <p>Typed questions use fixed keyword rules (no AI model). Read-only tools run at once; anything else waits for your confirmation. Typed chat never applies tags.</p></div>
      </div>
      <div className="chips">
        {EXAMPLES.map((q) => <button key={q} className="chip" onClick={() => setQuestion(q)}>{q}</button>)}
      </div>
      <div className="messages">
        {msgs.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            <p>{m.text}</p>
            {m.plan?.kind === "call" && (
              <div className="plan">
                <div><span className="muted small">Tool</span> <code>{m.plan.tool}</code></div>
                <div><span className="muted small">Arguments</span> <code>{JSON.stringify(m.plan.args)}</code>{Object.keys(m.plan.args).length === 0 && <span className="muted small"> (server defaults)</span>}</div>
                {m.plan.note && <p className="plan-note small">{m.plan.note}</p>}
                {m.execId === undefined ? (
                  <div className="plan-actions">
                    <button className="primary" disabled={busy} onClick={() => confirm(m)}>Confirm and call</button>
                    <button className="secondary" onClick={() => openInExplorer((m.plan as { tool: string }).tool, (m.plan as { args: Record<string, unknown> }).args)}>Edit arguments</button>
                  </div>
                ) : (
                  <>
                    {m.cached && (
                      <div className="cache-note" role="status">
                        <span className="pill info">Reused answer</span>
                        <span className="muted small">Cached response from {m.cached.age}; no server or AWS call was made.</span>
                        <button className="secondary" disabled={busy} onClick={() => confirm(m, "refresh")}>Refresh data</button>
                      </div>
                    )}
                    {m.answer && <p className="answer" aria-label="CloudGuard answer">{m.answer}</p>}
                    {execs.filter((x) => x.id === m.execId).map((x) => <ResultView key={x.id} ex={x} />)}
                  </>
                )}
              </div>
            )}
            {m.plan?.kind === "needs" && m.plan.missing === "region" && (
              <div className="plan">
                <p>{m.plan.prompt}</p>
                <div className="chips region-chips" aria-label="Region choices">
                  {regionChoices.map((r) => (
                    <button key={r.code} className="chip" onClick={() => submit(r.label)}>{r.label} <span className="chip-code">{r.code}</span></button>
                  ))}
                  {m.plan.prompt.includes("all regions") && <button className="chip" onClick={() => submit("All regions")}>All supported regions</button>}
                </div>
                <p className="muted small">Or type a region name or code, such as Ireland or eu-west-1.</p>
              </div>
            )}
            {m.plan?.kind === "needs" && m.plan.missing === "resource_name" && (
              <div className="plan">
                <p>{m.plan.prompt}</p>
                {m.options && m.options.length > 0 && (
                  <div className="chips">{m.options.map((n) => <button key={n} className="chip" onClick={() => submit(n)}>{n}</button>)}</div>
                )}
                <p className="muted small">Say or type the resource's name.</p>
              </div>
            )}
            {m.plan?.kind === "needs" && m.plan.missing !== "region" && m.plan.missing !== "resource_name" && (
              <div className="plan">
                <p>{m.plan.prompt}</p>
                <button className="secondary" onClick={() => openInExplorer((m.plan as { tool: string }).tool, (m.plan as { args: Record<string, unknown> }).args)}>Open {m.plan.tool} to provide {m.plan.missing}</button>
              </div>
            )}
            {m.flow?.stage === "confirm" && currentFlow?.id === m.flow.id && currentFlow.version === m.flow.version
              && currentFlow.stage === "confirm" && (
              <div className="plan-actions">
                <button className="primary" onClick={() => confirmFlow(m.flow!.version)}>Yes, prepare the proposal</button>
                <button className="secondary" onClick={() => submit("no")}>No</button>
              </div>
            )}
            {m.flow?.stage === "ready" && m.flow.preview?.request != null && (
              <div className="plan" aria-label="Proposal request">
                <p className="notice info">Preview only. Nothing has been applied: saving, approval by a different person and execution by someone who is not the approver happen in the CLI. Save this as <code>request.json</code>.</p>
                <pre className="request-json">{JSON.stringify(m.flow.preview.request, null, 2)}</pre>
                <pre className="request-json">{handoffCommands(m.flow).join("\n")}</pre>
              </div>
            )}
            {m.plan?.kind === "ambiguous" && (
              <div className="chips">{m.plan.candidates.map((t) => <button key={t} className="chip" onClick={() => openInExplorer(t, {})}>{t}</button>)}</div>
            )}
          </div>
        ))}
      </div>
      <form className="ask" onSubmit={(e) => { e.preventDefault(); submit(question); }}>
        <input value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="Ask about your AWS bill or resource tagging…" autoComplete="off" />
        <button className="primary" type="submit">Ask</button>
      </form>
    </section>
  );
}
