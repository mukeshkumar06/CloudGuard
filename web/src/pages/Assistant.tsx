import type { ReactNode } from "react";

import { ResultView, type Execution } from "../components/ResultView";
import { useVoiceState, VoicePanel } from "../components/VoicePanel";
import { VoiceTaggingPanel } from "../components/VoiceTaggingPanel";
import { STAGE_LABEL, type DiscoveryStage } from "../lib/discovery";
import { regionLabel } from "../lib/regions";
import type { VoiceController } from "../lib/voice";
import { Card, PageHead, Stepper } from "../ui/kit";

/** One spoken exchange in this browser session (memory only): what was heard, if new, and CloudGuard's reply. */
export interface VoiceTurn { id: number; at: Date; you?: string; final: boolean; reply: string; stage?: DiscoveryStage }

const STAGES: DiscoveryStage[] = ["UNDERSTAND_REQUEST", "CONFIRM_INTERPRETATION", "COLLECT_REGION", "DISCOVER_RESOURCES",
  "DISPLAY_RESULTS"];

/** Where the resource-discovery conversation is: the original request, the stage, and the region once known. */
function DiscoveryTracker({ voice }: { voice: VoiceController }) {
  const s = useVoiceState(voice);
  const d = s.discovery;
  if (!d) return null;
  const steps = d.tagIntent ? STAGES : STAGES.filter((x) => x !== "CONFIRM_INTERPRETATION");
  return (
    <Card title="Resource discovery" subtitle={<>Request: “{d.request}”</>} label="Resource discovery" className="discovery-card">
      <Stepper steps={steps.map((x) => STAGE_LABEL[x])} at={steps.indexOf(d.stage)} label="Discovery stage" />
      <dl className="kv">
        <dt>Resource type</dt><dd>{d.services.length ? d.services.join(", ") : "all supported types"}</dd>
        {d.filter && <><dt>Name filter</dt><dd>{d.filter.match.replace("_", " ")} “{d.filter.text}”</dd></>}
        <dt>Region</dt><dd>{d.region ? (d.region === "all" ? "All supported regions" : `${regionLabel(d.region)} (${d.region})`)
          : <span className="pill warn">Waiting for your answer</span>}</dd>
      </dl>
      {d.stage === "COLLECT_REGION" && <p className="small muted">Say a region, for example “Mumbai”, “US East” or “ap-southeast-1”{d.tagIntent ? "" : ", or “all regions”"}. Nothing is searched until you do.</p>}
    </Card>
  );
}

/** The voice-first home: the microphone, the typed conversation, voice tagging, and the answers as they arrive. */
export function Assistant({ voice, conversation, operator, execs, voiceLog }: {
  voice: VoiceController; conversation: ReactNode; operator: ReactNode; execs: Execution[]; voiceLog: VoiceTurn[];
}) {
  return (
    <div className="page">
      <PageHead eyebrow="Act" title="Voice Assistant" intro="Ask about costs, resources and tagging by voice or text. Answers come from CloudGuard's tools; spoken answers are the tool's own speech." />
      <div className="assistant-grid">
        <div className="assistant-main">
          <VoicePanel controller={voice} />
          <DiscoveryTracker voice={voice} />
          <Card title="Conversation history" subtitle="Spoken exchanges in this browser session (memory only)" label="Conversation history">
            {voiceLog.length === 0 ? <div className="empty">Tap the microphone and ask, for example, “List all Lambda functions”.</div> : (
              <ol className="history">
                {[...voiceLog].reverse().map((t) => (
                  <li key={t.id}>
                    {t.you && <p className="turn you"><span className="label">You</span>“{t.you}”{!t.final && <span className="pill warn">not final</span>}</p>}
                    <p className="turn cg"><span className="label">CloudGuard</span>{t.reply}</p>
                    <span className="small muted">{t.at.toLocaleTimeString()}{t.stage ? ` · ${STAGE_LABEL[t.stage]}` : ""}</span>
                  </li>
                ))}
              </ol>
            )}
          </Card>
        </div>
        <div className="assistant-side">
          {operator}
          <VoiceTaggingPanel controller={voice} />
          <section className="tool-pane card" aria-label="Tool explorer and results">
            <h2 className="history-title">Latest results</h2>
            {execs.length === 0 ? <div className="empty">No tool has been called yet.</div> : execs.slice(0, 3).map((x) => <ResultView key={x.id} ex={x} />)}
            {execs.length > 3 && <p className="small muted">Older results are in Activity &amp; Audit and the MCP Explorer.</p>}
          </section>
        </div>
      </div>
      {conversation}
    </div>
  );
}
