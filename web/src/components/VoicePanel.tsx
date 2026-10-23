import { useEffect, useState } from "react";

import { regionChoices } from "../lib/regions";
import { LOW_CONFIDENCE, RECOGNITION_LANGS, type VoiceController, type VoiceState } from "../lib/voice";
import type { VoicePreference } from "../lib/voices";

// The preference lives in the controller's memory for this page only: the simulator never uses browser storage.
const PREFS: { value: VoicePreference; label: string }[] = [
  { value: "female", label: "Female" }, { value: "male", label: "Male" }, { value: "system", label: "System default" },
];

function voiceNote(s: VoiceState): string {
  const kind = s.voicePreference === "female" ? "female" : "male";
  if (s.voicePreference === "system") return "Browser default voice";
  if (s.voiceMatch === "preferred") return s.voiceName!;
  if (s.voiceMatch === "english-fallback") return `No ${kind} English voice found; using ${s.voiceName}`;
  return s.voicesAvailable === 0 ? "Loading voices… using the browser default for now" : "No English voice found; using the browser default";
}

const STATUS_TEXT: Record<VoiceState["status"], string> = {
  idle: "Stopped. Tap the microphone and ask a question",
  starting: "Starting the microphone…",
  listening: "Listening… (up to a minute; pause when you're done)",
  transcript_received: "Processing…",
  awaiting_confirmation: "Waiting for your confirmation",
  executing: "Processing: asking CloudGuard…",
  speaking: "Speaking",
  error: "Something went wrong",
};

const CALL_TEXT = { running: "Running", ok: "Answered by", error: "Failed:", cached: "Reused cached answer from" } as const;

export function useVoiceState(controller: VoiceController): VoiceState {
  const [state, setState] = useState(controller.snapshot);
  useEffect(() => controller.subscribe(setState), [controller]);
  return state;
}

/** Fix a misheard request by typing it. Only a new request: it can't answer a question or confirmation in progress,
 *  and a tagging request still needs a spoken yes to its read-back, then the spoken confirmation of the plan. */
function Correction({ controller, s, listening, busy }: { controller: VoiceController; s: VoiceState; listening: boolean; busy: boolean }) {
  const [text, setText] = useState<string | null>(null);
  const open = !listening && !busy && !s.pending && !!s.transcript && (s.heardCheck !== null || (!s.writeFlow && !s.tagFlow && !s.clarifying));
  if (!open) return null;
  if (text === null) {
    return <p className="small"><button type="button" className="link" onClick={() => setText(s.transcript)}>Misheard? Correct the request</button></p>;
  }
  return (
    <form className="ask" aria-label="Correct the recognized request"
          onSubmit={(e) => { e.preventDefault(); if (controller.correctTranscript(text)) setText(null); }}>
      <input value={text} onChange={(e) => setText(e.target.value)} aria-label="Corrected request" autoComplete="off" />
      <button type="submit" className="secondary">Use this</button>
      <button type="button" className="link" onClick={() => setText(null)}>Cancel</button>
    </form>
  );
}

export function VoicePanel({ controller }: { controller: VoiceController }) {
  const s = useVoiceState(controller);
  // "starting": recognition was requested but the browser has not opened the microphone yet (no onstart).
  const listening = s.status === "listening" || s.status === "starting";
  const busy = s.status === "executing";
  return (
    <section className={`voice voice-${s.status}`} aria-label="Voice assistant" data-status={s.status}>
      <div className="voice-main">
        <div className="mic-wrap">
          <button
            type="button"
            className="mic"
            aria-label={listening ? "Stop listening" : "Start voice input"}
            aria-pressed={listening}
            disabled={busy || !s.recognitionSupported}
            onClick={() => (listening ? controller.stopListening() : controller.startListening())}
          >
            <span className="mic-ring" aria-hidden />
            <span className="mic-ring delay" aria-hidden />
            <svg viewBox="0 0 24 24" width="30" height="30" aria-hidden>
              <path fill="currentColor" d="M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-2.08A7 7 0 0 0 19 12h-2Z" />
            </svg>
          </button>
        </div>
        <div className="voice-text">
          <div className="voice-status" role="status" aria-live="polite">
            <span className={`state-dot ${s.status}`} aria-hidden />
            {s.status === "listening" && s.listenMode === "confirmation" ? "Listening for confirmation…" : STATUS_TEXT[s.status]}
            {busy && <span className="dots" aria-hidden><i /><i /><i /></span>}
            {s.status === "speaking" && <span className="wave" aria-hidden><i /><i /><i /><i /><i /></span>}
          </div>
          {(listening || s.transcript) && (
            <p className="voice-transcript" aria-label="Recognized speech">
              <span className="label">You {listening ? <span className="pill muted-pill">Interim · live</span>
                : s.transcriptFinal ? <span className="pill ok">Final</span> : null}</span>
              {listening ? <em>{s.interim || "…"}</em> : <>“{s.transcript}”</>}
              {!listening && s.transcript && !s.transcriptFinal && <> <span className="pill warn">Not final: not acted on</span></>}
              {!listening && s.transcript && s.transcriptFinal && s.transcriptConfidence !== null && s.transcriptConfidence < LOW_CONFIDENCE
                && <> <span className="pill warn">Low confidence</span></>}
            </p>
          )}
          <Correction controller={controller} s={s} listening={listening} busy={busy} />
          {s.message && (
            <p className="voice-reply" aria-label="CloudGuard reply">
              <span className="label">CloudGuard</span>
              {s.message}
            </p>
          )}
          {s.clarifying?.missing === "region" && (
            <div className="region-hint" aria-label="Regions you can say">
              <span className="label">Say a region</span>
              <div className="chips">{regionChoices().map((r) => <span key={r.code} className="chip static">{r.label} <span className="chip-code">{r.code}</span></span>)}
                {!s.clarifying.tagIntent && <span className="chip static">All regions</span>}</div>
            </div>
          )}
          {s.error && (
            <p className="voice-error-text" role="alert">
              {s.error}
              {s.recognitionSupported && !listening && !busy && (
                <> <button type="button" className="link" onClick={() => controller.startListening()}>Try again</button></>
              )}
            </p>
          )}
          {s.lastCall && (
            <p className={`voice-call ${s.lastCall.outcome}`}>
              {CALL_TEXT[s.lastCall.outcome]} <code>{s.lastCall.tool}</code>
              {s.lastCall.outcome !== "running" && <span className="muted"> · full result in the Results panel</span>}
            </p>
          )}
          {s.cached && (
            <p className="cache-note" role="status">
              <span className="pill info">Reused answer</span>
              <span className="muted small">from {s.cached.age}; no server or AWS call was made.</span>
              <button type="button" className="secondary" disabled={busy || listening || !!s.pending}
                      onClick={() => controller.refresh()}>Refresh data</button>
            </p>
          )}
          {!s.recognitionSupported && (
            <p className="muted small">Voice input needs a browser with the Web Speech API (Chrome or Edge). You can still type below.</p>
          )}
        </div>
      </div>

      {listening && (
        <div className="voice-actions">
          <button type="button" className="secondary" disabled={s.status !== "listening"} onClick={() => controller.stopListening()}>Done — stop listening</button>
          <button type="button" className="secondary" onClick={() => controller.cancelListening()}>Cancel listening</button>
        </div>
      )}

      {s.pending && (
        <div className="confirm-card" role="group" aria-label="Pending tool call">
          <p className="confirm-prompt">{s.pending.prompt}</p>
          <div className="small"><span className="muted">Tool</span> <code>{s.pending.tool}</code> <code>{JSON.stringify(s.pending.args)}</code></div>
          {s.pending.note && <p className="plan-note small">{s.pending.note}</p>}
          <p className="small muted">Say “yes”, “confirm” or “go ahead” to run it, or “no” / “cancel”. Nothing runs until you confirm.</p>
          <div className="voice-actions">
            <button type="button" className="primary" disabled={busy} onClick={() => controller.confirm(s.pending!.id)}>Yes, run it</button>
            <button type="button" className="secondary" onClick={() => controller.cancel()}>Cancel</button>
            <button type="button" className="secondary" disabled={!s.recognitionSupported || listening} onClick={() => controller.startListening()}>Answer by voice</button>
          </div>
        </div>
      )}

      {s.synthesisSupported && (
        <div className="voice-picker" role="group" aria-label="CloudGuard voice">
          <span className="label">Voice</span>
          <div className="segmented">
            {PREFS.map((p) => (
              <button key={p.value} type="button" aria-pressed={s.voicePreference === p.value}
                      onClick={() => controller.setVoicePreference(p.value)}>
                {p.label}
              </button>
            ))}
          </div>
          <button type="button" className="link" disabled={s.muted || listening || busy} onClick={() => controller.preview()}>Preview voice</button>
          <span className="muted small voice-name" data-match={s.voiceMatch}>{voiceNote(s)}</span>
        </div>
      )}

      <div className="voice-controls">
        <button type="button" className="link" disabled={!s.lastSpoken || s.muted || listening || busy} onClick={() => controller.replay()}>Replay</button>
        <button type="button" className="link" disabled={s.status !== "speaking"} onClick={() => controller.stopSpeaking()}>Stop speaking</button>
        <button type="button" className="link" aria-pressed={s.muted} disabled={!s.synthesisSupported} onClick={() => controller.setMuted(!s.muted)}>{s.muted ? "Unmute" : "Mute"}</button>
        {s.recognitionSupported && (
          <label className="toggle small">
            <span className="muted">Recognize</span>
            <select value={s.recognitionLang} aria-label="Speech recognition language" disabled={listening}
                    onChange={(e) => controller.setRecognitionLang(e.target.value)}>
              {RECOGNITION_LANGS.map((l) => <option key={l.lang} value={l.lang}>{l.label}</option>)}
            </select>
          </label>
        )}
        {s.recognitionSupported && (
          <label className="toggle small" title="Listen automatically after a confirmation prompt and after each answer. Every tool call still needs your yes.">
            <input type="checkbox" checked={s.autoListen} onChange={(e) => controller.setAutoListen(e.target.checked)} />
            Auto-listen
          </label>
        )}
        {s.muted && <span className="muted small">Muted: replies are shown as text only.</span>}
        {!s.synthesisSupported && <span className="muted small">Spoken output isn't available in this browser; replies are shown as text.</span>}
      </div>
    </section>
  );
}
