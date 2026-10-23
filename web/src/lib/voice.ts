// Browser voice interaction for the simulator: Web Speech recognition in, speechSynthesis out.
//
// The controller is a small state machine with the browser APIs injected, so it can be tested with fakes. It never
// sees the bearer token: tool calls go through the `execute` callback (the app's existing MCP pipeline), and only
// the server's `speech` field or the controller's own fixed prompts are ever spoken. Audio is never recorded,
// uploaded or stored by this code; the browser's recognition service turns it into text.
//
// Safety rules:
// - Read-only tools (deps.needsConfirmation returns false) run as soon as a question is matched: no "Shall I
//   continue?". Any other tool, and every tool when needsConfirmation is not given, is a pending call that asks
//   "Shall I continue?" and runs once, on an explicit "yes" (spoken or the button), for that pending call only.
// - A call runs only on an answer made of nothing but confirmation phrases ("yes", "yes yes", "okay, go ahead");
//   any refusal cancels it, and anything else ("yes, but check this month") leaves it pending (see confirm.ts).
// - Intent matching is the existing deterministic mapIntent over the tools the server listed.
// - Hands-free mode only starts LISTENING automatically (after a prompt or an answer finishes). It never confirms:
//   every call still needs its own spoken or clicked yes. One recognition session at most; each is bound to the
//   call pending when it started, and late events from an ended session are ignored.

import { parseConfirmation } from "./confirm";
import { answerRegion, answerResourceName, mapIntent, type Plan } from "./intent";
import { discoveryFor, isListing, regionAnswer, regionOnly, searchingText, withRegion,
         type DiscoveryContext } from "./discovery";
import { regionLabel, regionQuestion } from "./regions";
import { sayNames } from "./resources";
import { awaitingAnswer, filterWords, onAnswer, onPreviewResult, onSearchResult, serviceLabel, startTagFlow, type FlowStep,
         type TagFlow, type TagIntent } from "./tagflow";
import type { OperatorApi } from "./operator";
import { awaitingWrite, busyWrite, onConfirm, onPrepare, onWriteAnswer, onWriteSearch, startWrite, type WriteFlow,
         type WriteStep } from "./voicewrite";
import { chooseVoice, type VoiceChoice, type VoiceLike, type VoicePreference } from "./voices";

export const PREVIEW_TEXT = "Hello, I'm CloudGuard. This is how I will sound when I answer you.";

// Every utterance (prompts, server speech, replay, preview) gets exactly these, explicitly: the browser defaults are
// the same numbers, but leaving them unset lets an engine or a reused object carry other values.
export const SPEECH_SETTINGS = { volume: 1, rate: 1, pitch: 1 } as const;

export const LISTEN_TIMEOUT_MS = 60_000; // one listening session, from the first onstart, across browser restarts
export const SILENCE_MS = 2500; // after the last words, this much silence ends the answer (shorter pauses don't)
export const SPEAKING_GRACE_MS = 5000; // extra time after speech is detected before any words arrive
export const MAX_RESTARTS = 20; // browser-initiated ends restarted within one session, at most (no restart loops)
export const START_TIMEOUT_MS = 5000; // the browser must open the microphone (onstart) within this
export const SPEAKING_MAX_MS = LISTEN_TIMEOUT_MS; // the longest answer: it is wrapped up when the window ends
export const STOP_GRACE_MS = 1500; // after stop(), wait this long for the final words before using what we have
export const AUTO_LISTEN_DELAY_MS = 400; // gap after CloudGuard stops talking, so it doesn't hear its own echo
export const SPEECH_POLL_MS = 250; // how often to check whether the speech engine has gone quiet
// If the engine never reports the end of an utterance, assume it is done after this long (scaled by length).
export const speechMaxMs = (text: string) => 6000 + text.length * 120;
export const RECOGNITION_LANGS = [
  { lang: "en-IN", label: "English (India)" }, { lang: "en-US", label: "English (US)" },
  { lang: "en-GB", label: "English (UK)" },
] as const;
export const FOLLOW_UP_TEXT = "Would you like to know anything else?";
export const REPEAT_TEXT = "Sorry, I didn't catch a yes or no.";
export const CANCELLED_TEXT = "Okay, cancelled. Nothing was run.";
export const CACHED_INTRO = (age: string) => `I already have this answer from ${age}, so I didn't ask AWS again.`;

function ageWords(ms: number): string {
  const min = Math.floor(ms / 60_000);
  return min < 1 ? "less than a minute ago" : min === 1 ? "a minute ago" : `${min} minutes ago`;
}
// Below this, Chrome's own confidence in a final transcript is "unsure" (it reports 0..1; 0 means not reported).
export const LOW_CONFIDENCE = 0.6;
export const PARTIAL_TEXT = "I only caught part of that, so I won't act on it.";
export const REQUEST_AGAIN_TEXT = "Okay, nothing was done. Say the whole request again.";
export const CONFIRM_TIMEOUT_TEXT = "Didn't hear you. Say yes or no, or try again.";
export const QUESTION_TIMEOUT_TEXT = "Didn't hear a question. Tap the microphone or Try again to ask.";

export type ListenMode = "question" | "confirmation";

/** One listening session, which may span several browser recognizers (restarts). */
interface ListenSession {
  mode: ListenMode;
  interimText: string; // the latest not-yet-final words, kept in case the session ends before they turn final
  heard: boolean; // speech was detected or words arrived
  restarts: number; // browser-initiated ends restarted so far
  userStopped: boolean; // "Done" / Stop listening: finish with what was heard, never restart
  opened: boolean; // the microphone opened at least once (the window timer runs)
  silentEnd?: boolean; // the browser reported no-speech; its onend restarts
  wrapUp?: () => void; // stop gracefully and finish with what was heard
}

export interface Timers {
  set(fn: () => void, ms: number): unknown;
  clear(handle: unknown): void;
}

export type VoiceStatus = "idle" | "starting" | "listening" | "transcript_received" | "awaiting_confirmation"
  | "executing" | "speaking" | "error";

export interface PendingCall {
  id: number;
  tool: string;
  args: Record<string, unknown>;
  prompt: string;
  note?: string;
  refresh?: boolean; // a Refresh: bypass the answer cache
}

/** A previous answer found in the conversation cache (no server call). */
export interface CacheHit {
  speech: string;
  ageMs: number;
  resource?: ResourceRef;
}

export interface VoiceState {
  status: VoiceStatus;
  interim: string; // words heard so far (not final)
  transcript: string; // last transcript handed on
  transcriptFinal: boolean; // false: the recognizer ended before finalising it (interim words only)
  transcriptConfidence: number | null; // the lowest confidence the browser reported for its final parts
  // A spoken tagging request, read back as heard and as understood, waiting for a spoken yes before any search.
  // region is unset when the request named none: it is asked for after the yes, never taken from a default.
  heardCheck: { text: string; intent: TagIntent; region?: string; prompt: string } | null;
  pending: PendingCall | null;
  message: string; // what CloudGuard said or is saying (shown as text too)
  lastSpoken: string; // for replay
  error: string | null;
  lastCall: { tool: string; outcome: "running" | "ok" | "error" | "cached" } | null; // the last voice answer's source
  cached: { tool: string; args: Record<string, unknown>; age: string } | null; // the reply reused a cached answer
  // A question waiting for its region or its resource ("Which Lambda function?"): the next answer fills it in.
  // `request` is the user's original words; `tagIntent` is a confirmed tagging request waiting for its region.
  clarifying: { tool: string; args: Record<string, unknown>; reason: string; prompt: string;
                missing: "region" | "resource_name"; request?: string; tagIntent?: TagIntent } | null;
  // The resource-discovery conversation (discovery.ts): its stage, the original request, the region once known.
  discovery: DiscoveryContext | null;
  lastResource: ResourceRef | null; // what "it" means in a follow-up ("What tags should I add?")
  tagFlow: TagFlow | null; // a group tagging conversation (tagflow.ts); it can only ever reach a read-only preview
  writeFlow: WriteFlow | null; // personal voice tagging (voicewrite.ts): only with an operator session, only by voice
  muted: boolean;
  recognitionSupported: boolean;
  synthesisSupported: boolean;
  voicePreference: VoicePreference;
  voiceName: string | null; // the voice in use; null = the browser default
  voiceMatch: VoiceChoice["match"];
  voicesAvailable: number; // voices the browser has reported so far (often 0 until "voiceschanged")
  autoListen: boolean; // hands-free: listen after confirmation prompts and after each answer
  listenMode: ListenMode | null; // what the current listening session is for
  recognitionLang: string; // BCP 47 language for speech recognition, e.g. en-IN
}

// Minimal shapes of the Web Speech APIs this controller uses.
export interface RecognitionLike {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  maxAlternatives: number;
  onresult: ((e: { resultIndex: number; results: ArrayLike<ArrayLike<{ transcript: string; confidence?: number }> & { isFinal: boolean }> }) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  onstart?: (() => void) | null; // the microphone is open and audio is being captured
  onspeechstart?: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

export interface UtteranceLike {
  text: string;
  lang: string;
  voice?: VoiceLike | null;
  volume?: number;
  rate?: number;
  pitch?: number;
  onstart?: (() => void) | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
}

export interface SynthLike {
  speak(u: UtteranceLike): void;
  cancel(): void;
  readonly speaking?: boolean;
  readonly pending?: boolean;
  getVoices?(): VoiceLike[];
  addEventListener?(type: "voiceschanged", fn: () => void): void;
  removeEventListener?(type: "voiceschanged", fn: () => void): void;
}

export interface ExecuteResult {
  speech?: string; // the server's speech field (with safe resource names added by the app), when the call succeeded
  isError?: boolean; // the tool returned isError
  errorText?: string; // a sanitized message for a failed call or tool error
  resource?: ResourceRef; // the one resource this answer is about, remembered for "What tags should I add?"
}

/** A resource the conversation is about: its finding ID (for the tools) and its safe display name, if any. */
export interface ResourceRef {
  finding_id: string;
  name?: string;
  service?: string;
}

export interface VoiceDeps {
  createRecognition: (() => RecognitionLike) | null; // null: not supported in this browser
  synth: SynthLike | null;
  createUtterance: ((text: string) => UtteranceLike) | null;
  tools: () => string[]; // tools the server listed
  execute: (tool: string, args: Record<string, unknown>, opts?: { refresh?: boolean }) => Promise<ExecuteResult>;
  // The app's answer cache, read-only from here: a hit is spoken without any server call. Optional (no cache).
  lookup?: (tool: string, args: Record<string, unknown>) => CacheHit | null;
  // Safe display names of resources the user could mean ("Which Lambda function?"), from a read-only tool result.
  candidateNames?: (services?: string[]) => Promise<string[]>;
  // False only for read-only tools, which then run without asking. Omitted: every call needs a yes (fails closed).
  needsConfirmation?: (tool: string) => boolean;
  // A read-only call whose structured (Alexa-projected) result the tagging conversation reads. Optional: without it
  // the conversation explains that it can't run.
  call?: (tool: string, args: Record<string, unknown>) => Promise<{ structured?: unknown; errorText?: string }>;
  // The user's preferred region. Display only: a request that names no region is always asked "which region?";
  // this is never used in its place.
  selectedRegion?: () => string;
  // Whether a listing tool can search every region (from its tools/list input schema). Omitted: it can.
  regionSupport?: (tool: string) => { allRegions: boolean };
  // Personal voice tagging, when the server enables it and an operator session is active (HttpOnly cookie; this
  // controller never sees a credential). null: tagging requests stay preview-only. Never used by typed chat.
  operator?: () => OperatorApi | null;
  timers?: Timers; // injectable for tests; defaults to setTimeout
}

export { confirmationOf, normalizeUtterance, parseConfirmation } from "./confirm";

const MONTH_PHRASE: Record<string, string> = { "last-month": "last month", "this-month": "this month" };

function monthPhrase(args: Record<string, unknown>): string {
  const m = typeof args.month === "string" ? args.month : undefined;
  return m ? ` for ${MONTH_PHRASE[m] ?? m}` : "";
}

/** A short spoken description of a pending call, from the tool name and arguments only (never server data). */
export function describeCall(tool: string, args: Record<string, unknown>): string {
  switch (tool) {
    case "get_monthly_aws_bill": return `check your AWS bill${monthPhrase(args) || " for last month"}`;
    case "get_current_month_spending": return "check your AWS spending so far this month";
    case "get_cost_breakdown": return `show which AWS services cost the most${monthPhrase(args)}`;
    case "compare_monthly_costs": return "compare your AWS costs between the last two months";
    case "investigate_cost_increase": return "investigate what changed in your AWS costs";
    case "get_cost_anomalies": return "check AWS Cost Anomaly Detection for unusual spending";
    case "get_resource_inventory": {
      const where = args.region === "all" || !args.region ? "in all regions" : `in ${regionLabel(String(args.region))}`;
      return `count your AWS resources ${where}`;
    }
    case "scan_tag_compliance": return "check your resources against the tagging policy";
    case "list_untagged_resources": return "list the resources that are missing mandatory tags";
    case "explain_finding": return "explain that finding";
    case "recommend_tags": return "recommend tag values for that resource";
    case "get_aws_account_identity": return "check which AWS account CloudGuard is using";
    default: return `run ${tool.replace(/_/g, " ")}`;
  }
}

function planMessage(plan: Exclude<Plan, { kind: "call" }>): string {
  switch (plan.kind) {
    case "needs": return `${plan.prompt} You can pick it in the tool panel.`;
    case "ambiguous": return "That could mean more than one thing. Please pick a tool in the panel, or ask more specifically.";
    case "unavailable": return "That tool isn't available for this connection.";
    default: return plan.reason;
  }
}

export class VoiceController {
  private state: VoiceState;
  private listeners = new Set<(s: VoiceState) => void>();
  private recognition: RecognitionLike | null = null;
  private finalText = "";
  private finalConfidence: number | null = null; // lowest reported confidence among this session's final parts
  private recognitionFailed = false;
  private utteranceSeq = 0;
  private pendingSeq = 0;
  private execSeq = 0; // bumped by reset(), so a call that finishes after a disconnect is not spoken
  private voices: VoiceLike[] = [];
  private voice: VoiceLike | null = null;
  private readonly onVoicesChanged = () => this.loadVoices();
  // The utterance being spoken. Holding it stops Chrome garbage-collecting it mid-sentence (which loses its end
  // event), and lets a late event from a cancelled one be recognised as stale.
  private current: UtteranceLike | null = null;
  private speechTimer: unknown = null; // fallback check for an utterance whose end event never arrives
  private listenTimer: unknown = null; // the current recognizer's timer (start, silence, stop grace)
  private windowTimer: unknown = null; // the whole listening window, across browser restarts
  private session: ListenSession | null = null; // the current listening session, if any
  private autoTimer: unknown = null; // the short gap between CloudGuard finishing speaking and auto-listening
  private readonly timers: Timers;

  constructor(private deps: VoiceDeps, voicePreference: VoicePreference = "female",
              options: { autoListen?: boolean; recognitionLang?: string } = {}) {
    this.timers = deps.timers ?? {
      set: (fn, ms) => globalThis.setTimeout(fn, ms),
      clear: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
    };
    this.state = {
      status: "idle", interim: "", transcript: "", transcriptFinal: true, transcriptConfidence: null, heardCheck: null,
      pending: null, message: "", lastSpoken: "", error: null, lastCall: null,
      muted: false,
      recognitionSupported: deps.createRecognition !== null,
      synthesisSupported: deps.synth !== null && deps.createUtterance !== null,
      voicePreference, voiceName: null, voiceMatch: "browser-default", voicesAvailable: 0,
      autoListen: options.autoListen ?? true, listenMode: null,
      recognitionLang: options.recognitionLang ?? "en-US", cached: null, clarifying: null, lastResource: null,
      tagFlow: null, writeFlow: null, discovery: null,
    };
    this.attach();
    this.loadVoices();
  }

  /** Listen for voice-list changes (Chrome and Edge report no voices until "voiceschanged"). Idempotent. */
  attach(): void {
    this.deps.synth?.addEventListener?.("voiceschanged", this.onVoicesChanged);
  }

  /** Stop listening for voice-list changes (page teardown). */
  dispose(): void {
    this.deps.synth?.removeEventListener?.("voiceschanged", this.onVoicesChanged);
  }

  // ---------- voice selection ----------

  private loadVoices(): void {
    try {
      this.voices = [...(this.deps.synth?.getVoices?.() ?? [])];
    } catch {
      this.voices = [];
    }
    this.applyPreference();
  }

  private applyPreference(): void {
    const choice = chooseVoice(this.voices, this.state.voicePreference);
    this.voice = choice.voice;
    this.set({ voiceName: choice.voice?.name ?? null, voiceMatch: choice.match, voicesAvailable: this.voices.length });
  }

  /** Takes effect from the next utterance; whatever is being said now is not cut off. */
  setVoicePreference(pref: VoicePreference): void {
    this.set({ voicePreference: pref });
    this.applyPreference();
  }

  /** Say a fixed sample in the selected voice. Not kept for replay and never shown as CloudGuard's reply. */
  preview(): void {
    if (this.state.muted || this.state.status === "executing" || this.isListening()) return;
    this.clearAutoTimer();
    this.speakNow(PREVIEW_TEXT, this.state.pending ? "awaiting_confirmation" : this.state.status === "error" ? "error" : "idle");
  }

  /** Language for the next recognition session (the current one, if any, keeps its language). */
  setRecognitionLang(lang: string): void {
    if (/^[a-z]{2}(-[A-Z]{2})?$/.test(lang)) this.set({ recognitionLang: lang });
  }

  /** Hands-free mode: listen automatically after a confirmation prompt and after each answer. */
  setAutoListen(on: boolean): void {
    if (!on) this.clearAutoTimer();
    this.set({ autoListen: on });
  }

  get snapshot(): VoiceState {
    return this.state;
  }

  subscribe(fn: (s: VoiceState) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private set(patch: Partial<VoiceState>) {
    this.state = { ...this.state, ...patch };
    for (const fn of this.listeners) fn(this.state);
  }

  // ---------- listening ----------

  /** Start listening: for the pending call's yes or no when there is one, else for a new question.
   *
   *  One listening session lasts up to LISTEN_TIMEOUT_MS (about a minute) from the first `onstart`, and may span
   *  several browser recognition objects: Chrome and Edge end recognition by themselves (after a few seconds of
   *  silence, or a network hiccup), so while nothing has been heard and the window is open, a browser-initiated end
   *  starts a fresh recognizer instead of giving up (at most MAX_RESTARTS times). Recognition is continuous, so a
   *  short pause doesn't end an answer: once words have arrived, SILENCE_MS without new words wraps it up. A manual
   *  stop uses what was heard; cancel discards it; neither restarts. Nothing heard is never an answer.
   *
   *  The panel shows "starting" until the browser fires `onstart` (the microphone is really open), then "listening".
   *  Once speech is detected it never aborts mid-sentence: it asks the recognizer to stop() gracefully so the final
   *  words still arrive. */
  startListening(): void {
    const status = this.state.status;
    if (this.recognition || status === "executing" || status === "listening" || status === "starting") return;
    this.clearAutoTimer();
    if (!this.deps.createRecognition) {
      this.set({ status: "error", error: "Voice input isn't supported in this browser. Use Chrome or Edge, or type your question." });
      return;
    }
    this.stopSpeaking(false); // never listen while CloudGuard is talking (it would hear itself)
    // A session is bound to the call pending when it started: its answer can only ever apply to that call.
    const forPending = this.state.pending?.id ?? null;
    const mode: ListenMode = forPending === null ? "question" : "confirmation";
    this.finalText = "";
    this.finalConfidence = null;
    this.recognitionFailed = false;
    const sess: ListenSession = { mode, interimText: "", heard: false, restarts: 0, userStopped: false, opened: false };
    this.session = sess;
    this.set({ status: "starting", listenMode: mode, interim: "", transcript: "", error: null });
    this.openRecognizer(sess, forPending);
  }

  /** One browser recognizer within a listening session (the first, or a restart after a browser-initiated end). */
  private openRecognizer(sess: ListenSession, forPending: number | null): void {
    let rec: RecognitionLike;
    try {
      rec = this.deps.createRecognition!();
    } catch {
      this.endSession();
      this.set({ status: "error", interim: "", error: "Voice input could not be started in this browser." });
      return;
    }
    rec.lang = this.state.recognitionLang;
    rec.interimResults = true;
    rec.continuous = true; // pauses don't end it; SILENCE_MS after the last words does
    rec.maxAlternatives = 1;
    const mode = sess.mode;
    const live = () => this.recognition === rec; // false for an aborted, timed-out, ended or replaced recognizer
    const finals = new Set<number>(); // result indexes already added: a final is never counted twice

    const arm = (ms: number, fn: () => void) => {
      if (this.listenTimer !== null) this.timers.clear(this.listenTimer);
      this.listenTimer = this.timers.set(() => { if (live()) fn(); }, ms);
    };
    // Use what was heard: the final transcript, else the last interim words. Ends the session first.
    const finish = () => {
      const text = (this.finalText || sess.interimText).trim();
      this.endSession();
      if (this.recognitionFailed) return;
      if (!text) return this.notHeard(mode);
      if (mode === "confirmation" && this.state.pending?.id !== forPending) {
        // The call this answer was for is gone (cancelled, confirmed by button, replaced): drop the answer.
        this.set({ status: this.state.pending ? "awaiting_confirmation" : "idle", interim: "" });
        return;
      }
      // Interim-only words (the recognizer ended before finalising them) are marked, never passed off as final.
      void this.handleTranscript(text, { final: Boolean(this.finalText), confidence: this.finalConfidence });
    };
    // Stop gracefully so pending words are finalised; if the recognizer never ends, use what we have.
    const wrapUp = () => {
      try { rec.stop(); } catch { /* already stopping */ }
      arm(STOP_GRACE_MS, () => { finish(); try { rec.abort(); } catch { /* ended */ } });
    };
    sess.wrapUp = wrapUp;
    const words = () => Boolean(this.finalText || sess.interimText);

    rec.onstart = () => {
      if (!live()) return;
      this.set({ status: "listening", listenMode: mode });
      if (this.listenTimer !== null) this.timers.clear(this.listenTimer); // the microphone did open
      this.listenTimer = null;
      if (!sess.opened) {
        sess.opened = true;
        // The whole listening window, across restarts. With words under way, wrap up instead of aborting.
        this.windowTimer = this.timers.set(() => {
          this.windowTimer = null;
          if (this.session !== sess || !this.recognition) return;
          if (sess.heard || words()) return sess.wrapUp?.();
          this.abortRecognition();
          this.notHeard(mode);
        }, LISTEN_TIMEOUT_MS);
      }
      if (sess.heard || words()) arm(SILENCE_MS, wrapUp); // a restarted recognizer mid-answer
    };
    rec.onspeechstart = () => {
      if (!live()) return;
      sess.heard = true;
      arm(SILENCE_MS + SPEAKING_GRACE_MS, wrapUp); // speech detected but no words yet
    };
    rec.onresult = (e) => {
      if (!live()) return;
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        const text = r[0]?.transcript ?? "";
        if (r.isFinal) {
          if (finals.has(i) || !text.trim()) continue;
          finals.add(i);
          const conf = r[0]?.confidence;
          if (typeof conf === "number" && conf > 0) {
            this.finalConfidence = this.finalConfidence === null ? conf : Math.min(this.finalConfidence, conf);
          }
          this.finalText = this.finalText ? `${this.finalText} ${text.trim()}` : text.trim();
        } else interim += text;
      }
      sess.interimText = interim;
      if (words()) {
        sess.heard = true;
        arm(SILENCE_MS, wrapUp); // a pause shorter than this keeps listening
      }
      this.set({ interim });
    };
    rec.onerror = (e) => {
      if (!live()) return;
      // Chrome can report "no-speech" or "aborted" after it already returned words: use them.
      if ((e.error === "no-speech" || e.error === "aborted") && words()) return finish();
      // Silence before anything was said is the browser giving up early: onend restarts while the window is open.
      if (e.error === "no-speech" && !sess.userStopped) {
        sess.silentEnd = true;
        return;
      }
      this.endSession();
      this.recognitionFailed = true;
      if (e.error === "no-speech") return this.notHeard(mode);
      // Permission denied and other failures: stop and say why, with no restart. A pending call stays pending.
      this.set({ status: "error", interim: "", error: recognitionErrorMessage(e.error) });
    };
    rec.onend = () => {
      if (!live()) return;
      if (sess.userStopped || words()) return finish(); // the answer is complete (or the user said "done")
      // The browser ended the recognizer with nothing heard: keep listening while the window is open.
      sess.silentEnd = false;
      if (sess.opened && this.windowTimer !== null && sess.restarts < MAX_RESTARTS) {
        sess.restarts += 1;
        this.openRecognizer(sess, forPending);
        return;
      }
      finish();
    };

    this.recognition = rec;
    // The browser never opened the microphone (blocked prompt, busy device, no speech service). Armed before
    // start(), because onstart may fire synchronously.
    arm(START_TIMEOUT_MS, () => {
      if (this.state.status !== "starting") return;
      this.abortRecognition();
      this.set({ status: "error", interim: "",
                 error: "The microphone didn't start. Check that this page may use the microphone, then Try again." });
    });
    try {
      rec.start();
    } catch {
      this.endSession();
      this.set({ status: "error", interim: "",
                 error: "The microphone is busy or could not be started. Close other tabs using it, then Try again." });
    }
  }

  /** Nothing usable was heard (timeout, silence or an empty transcript). Never confirms or cancels anything. */
  private notHeard(mode: ListenMode): void {
    if (mode === "confirmation" && this.state.pending) {
      this.set({ status: "awaiting_confirmation", interim: "", error: CONFIRM_TIMEOUT_TEXT });
    } else {
      this.set({ status: "idle", interim: "", error: QUESTION_TIMEOUT_TEXT });
    }
  }

  /** Finish listening and use what was heard (never restarts). Nothing heard is "didn't hear you", never an answer. */
  stopListening(): void {
    const sess = this.session;
    if (!this.recognition || !sess) return;
    sess.userStopped = true;
    sess.wrapUp?.();
  }

  /** Stop listening and discard what was heard. */
  cancelListening(): void {
    if (!this.recognition) return;
    this.abortRecognition();
    this.set({ status: this.state.pending ? "awaiting_confirmation" : "idle", interim: "" });
  }

  private isListening(): boolean {
    return this.state.status === "listening" || this.state.status === "starting";
  }

  private endSession(): void {
    this.recognition = null;
    this.session = null; // a late event from any recognizer of this session is ignored, and nothing restarts
    if (this.listenTimer !== null) this.timers.clear(this.listenTimer);
    this.listenTimer = null;
    if (this.windowTimer !== null) this.timers.clear(this.windowTimer);
    this.windowTimer = null;
    this.set({ listenMode: null });
  }

  private abortRecognition(): void {
    const rec = this.recognition;
    if (!rec) return;
    this.endSession();
    rec.abort();
  }

  private clearAutoTimer(): void {
    if (this.autoTimer !== null) this.timers.clear(this.autoTimer);
    this.autoTimer = null;
  }

  /** After CloudGuard finishes speaking, listen again if hands-free mode is on and nothing else has happened since. */
  private autoListenAfter(pendingId: number | null): () => void {
    return () => {
      if (!this.state.autoListen || !this.state.recognitionSupported) return;
      this.clearAutoTimer();
      this.autoTimer = this.timers.set(() => {
        this.autoTimer = null;
        const status = this.state.status;
        if (!this.state.autoListen || this.recognition) return;
        if (status === "executing" || status === "listening" || status === "starting" || status === "speaking") return;
        // Never open the microphone while the engine still reports audio playing: on Windows an open microphone
        // can duck other output (Communications setting) or switch a Bluetooth headset to its low-quality profile.
        if (this.deps.synth?.speaking === true) return this.autoListenAfter(pendingId)();
        if ((this.state.pending?.id ?? null) !== pendingId) return;
        this.startListening();
      }, AUTO_LISTEN_DELAY_MS);
    };
  }

  // ---------- transcripts and confirmation ----------

  async handleTranscript(text: string, heard: { final?: boolean; confidence?: number | null } = {}): Promise<void> {
    const final = heard.final ?? true;
    this.set({ status: "transcript_received", transcript: text, transcriptFinal: final,
               transcriptConfidence: heard.confidence ?? null, interim: "", error: null });
    const pending = this.state.pending;
    // Words the recognizer never finalised can't confirm, cancel or answer anything in progress: ask again.
    if (!final && (pending || this.state.heardCheck || this.state.clarifying || awaitingWrite(this.state.writeFlow)
                   || awaitingAnswer(this.state.tagFlow))) {
      const ask = pending?.prompt ?? this.state.heardCheck?.prompt ?? this.state.clarifying?.prompt
        ?? this.state.writeFlow?.question ?? "Please say that again.";
      const after = pending ? "awaiting_confirmation" : "idle";
      this.set({ status: after });
      this.say(`${PARTIAL_TEXT} ${ask}`, after, this.autoListenAfter(pending?.id ?? null));
      return;
    }
    if (this.state.heardCheck && !pending) return this.answerHeardCheck(text);
    if (pending) {
      const { answer, rest } = parseConfirmation(text);
      // Same path as the "Yes, run it" button: confirm() checks the id and runs the call at most once.
      if (answer === "yes") return this.confirm(pending.id);
      if (answer === "no" && rest) {
        // "No, but show me the account": cancel first, then treat the rest as a new question (its own confirmation).
        this.abortRecognition();
        this.clearAutoTimer();
        this.set({ pending: null, interim: "" });
        return this.planQuestion(rest, CANCELLED_TEXT + " ");
      }
      if (answer === "no") return this.cancel();
      // Unclear or qualified answers never confirm; the same call stays pending and CloudGuard asks again.
      this.set({ status: "awaiting_confirmation" });
      this.say(`${REPEAT_TEXT} ${pending.prompt}`, "awaiting_confirmation", this.autoListenAfter(pending.id));
      return;
    }
    if (busyWrite(this.state.writeFlow)) return; // a repeated transcript while a write call runs does nothing
    if (awaitingWrite(this.state.writeFlow)) return void this.stepWrite(onWriteAnswer(this.state.writeFlow, text));
    if (awaitingAnswer(this.state.tagFlow)) return this.answerTagFlow(text);
    if (this.state.clarifying) return this.answerClarification(text);
    this.planQuestion(text);
  }

  /** The reply to "all regions or a specific region?" or "Which Lambda function?". A region ("Mumbai", "all") or a
   *  name ("OrderProcessor") runs the question it belongs to; a complete question of its own replaces it; "cancel"
   *  drops it; anything else asks again. */
  private answerClarification(text: string): void {
    const c = this.state.clarifying!;
    if (/^\s*(cancel|never ?mind|stop|forget it|no)\b/i.test(text)) {
      this.set({ clarifying: null, status: "idle", discovery: null });
      this.say(CANCELLED_TEXT, "idle", this.autoListenAfter(null));
      return;
    }
    // "Mumbai", "US East", "all regions", "what about Singapore": the answer to "Which region?", even when it also
    // reads like a question of its own ("search all regions"). The original request is kept and completed.
    if (c.missing === "region" && regionOnly(text) !== undefined) return this.answerRegionQuestion(c, text);
    // A short answer to "Which Lambda function?" is a name, even if it looks like a keyword ("billing-report").
    const shortName = c.missing === "resource_name" && text.trim().split(/\s+/).length <= 4 && !text.includes("?");
    const other = mapIntent(text, this.deps.tools());
    if (!shortName && (other.kind === "call" || other.kind === "needs")) {
      this.set({ clarifying: null });
      return this.planQuestion(text);
    }
    if (c.missing === "region") return this.answerRegionQuestion(c, text);
    const plan = answerResourceName(c, text);
    if (plan.kind === "call") {
      this.set({ clarifying: null });
      return this.dispatch(plan);
    }
    const prompt = plan.kind === "needs" ? plan.prompt : c.prompt;
    this.set({ status: "idle", clarifying: { ...c, prompt } });
    this.say(prompt, "idle", this.autoListenAfter(null));
  }

  private allRegions(tool: string): boolean {
    return this.deps.regionSupport?.(tool).allRegions ?? true;
  }

  /** COLLECT_REGION: the reply to "Which AWS region should I search?". A valid region completes the original
   *  request (the listing, or the confirmed tagging request); anything else asks again and runs nothing. */
  private answerRegionQuestion(c: NonNullable<VoiceState["clarifying"]>, text: string): void {
    if (c.tagIntent) {
      const answer = regionAnswer(text, { allRegions: false, oneRegionReason: "Tagging is done one region at a time." });
      if (answer.kind === "retry") return this.askRegionAgain(c, answer.prompt);
      const intent = { ...c.tagIntent, region: answer.region };
      const d = this.state.discovery;
      this.set({ clarifying: null, discovery: d ? { ...d, stage: "DISCOVER_RESOURCES", region: answer.region } : null });
      if (this.deps.operator?.()) {
        this.set({ tagFlow: null });
        const args = { text: intent.filter.text, match: intent.filter.match, services: [intent.service], region: answer.region };
        return void this.stepWrite(startWrite(intent, ++this.pendingSeq, answer.region),
                                   `${searchingText("search_resources", args)} `);
      }
      return void this.stepTagFlow(startTagFlow(intent, ++this.pendingSeq, answer.region));
    }
    const plan = answerRegion(c, text, { allRegions: this.allRegions(c.tool) });
    if (plan.kind !== "call") return this.askRegionAgain(c, plan.kind === "needs" ? plan.prompt : c.prompt);
    this.set({ clarifying: null });
    this.dispatch(plan, `${searchingText(plan.tool, plan.args)} `, c.request);
  }

  private askRegionAgain(c: NonNullable<VoiceState["clarifying"]>, prompt: string): void {
    this.set({ status: "idle", clarifying: { ...c, prompt } });
    this.say(prompt, "idle", this.autoListenAfter(null));
  }

  private needsConfirmation(tool: string): boolean {
    return this.deps.needsConfirmation?.(tool) ?? true;
  }

  /** Match a question to a tool. A read-only tool runs at once; any other tool only becomes a pending call. */
  private planQuestion(text: string, prefix = ""): void {
    this.set({ cached: null });
    // DISCOVER_RESOURCES or DISPLAY_RESULTS: "What about Singapore?" repeats the listing in the new region. It is a new
    // search in a new discovery context; a search still running for the old region is superseded (run() drops its
    // answer), and no answer from the previous region is reused.
    const last = this.state.discovery;
    const change = last && (last.stage === "DISCOVER_RESOURCES" || last.stage === "DISPLAY_RESULTS") && !last.tagIntent
      ? regionOnly(text) : undefined;
    if (last && change && change !== "ambiguous" && (change !== "all" || this.allRegions(last.tool))) {
      const args = withRegion(last.tool, last.args, change);
      return this.dispatch({ kind: "call", tool: last.tool, args, reason: "Same request, another region." },
                           `${prefix}${searchingText(last.tool, args)} `, last.request);
    }
    const plan = mapIntent(text, this.deps.tools());
    if (plan.kind === "needs" && plan.missing === "region") {
      // UNDERSTAND_REQUEST -> COLLECT_REGION: no region named (or one not understood). Keep the request, ask, and
      // run nothing until a valid region is given.
      this.set({ status: "idle", clarifying: { tool: plan.tool, args: plan.args, reason: plan.reason, prompt: plan.prompt,
                                               missing: "region", request: text },
                 discovery: discoveryFor(++this.pendingSeq, text, plan.tool, plan.args, "COLLECT_REGION") });
      this.say(prefix + plan.prompt, "idle", this.autoListenAfter(null));
      return;
    }
    if (plan.kind === "needs" && plan.missing === "resource_name") return void this.askWhichResource(plan, prefix);
    if (plan.kind === "call") return this.dispatch(plan, prefix);
    if (plan.kind === "tag") {
      // Speech recognition can mishear names ("CodeBridge" as "Godrej unit"). Before anything is searched, say
      // back exactly what was heard and how it was understood, and wait for a spoken yes. Words never finalised
      // by the recognizer are not acted on at all.
      if (!this.state.transcriptFinal) {
        this.set({ status: "idle" });
        this.say(`${prefix}${PARTIAL_TEXT} I heard: "${text}". Please say the whole request again.`, "idle",
                 this.autoListenAfter(null));
        return;
      }
      const region = plan.intent.region; // never a default: without one, the region is asked for after the yes
      const unsure = (this.state.transcriptConfidence ?? 1) < LOW_CONFIDENCE ? "I'm not sure I heard that correctly. " : "";
      const where = region ? ` in ${regionLabel(region)}` : "";
      const prompt = `${unsure}I heard: "${text}". That means ${serviceLabel(plan.intent.service)}${where} `
        + `with ${filterWords(plan.intent.filter)}${plan.intent.tag ? `, tagged ${plan.intent.tag.key} = ${plan.intent.tag.value}` : ""}. `
        + `Is that right? Say yes to ${region ? "look them up" : "choose the region"}, or no to say it again.`;
      const args = { text: plan.intent.filter.text, match: plan.intent.filter.match, services: [plan.intent.service],
                     ...(region ? { region } : {}) };
      this.set({ status: "idle", heardCheck: { text, intent: plan.intent, ...(region ? { region } : {}), prompt },
                 discovery: { ...discoveryFor(++this.pendingSeq, text, "search_resources", args, "CONFIRM_INTERPRETATION"),
                              tagIntent: plan.intent } });
      this.say(prefix + prompt, "idle", this.autoListenAfter(null));
      return;
    }
    this.set({ status: "idle" });
    this.say(prefix + planMessage(plan), "idle", this.autoListenAfter(null));
  }

  /** The spoken answer to "I heard ... Is that right?": only a clear yes starts the (read-only) search. */
  private answerHeardCheck(text: string): void {
    const check = this.state.heardCheck!;
    const { answer } = parseConfirmation(text);
    if (answer === "no") {
      this.set({ heardCheck: null, status: "idle", discovery: null });
      this.say(REQUEST_AGAIN_TEXT, "idle", this.autoListenAfter(null));
      return;
    }
    if (answer !== "yes") {
      this.set({ status: "idle" });
      this.say(`${REPEAT_TEXT} ${check.prompt}`, "idle", this.autoListenAfter(null));
      return;
    }
    this.set({ heardCheck: null });
    const d = this.state.discovery;
    if (!check.region) {
      // CONFIRM_INTERPRETATION -> COLLECT_REGION: the request is confirmed but names no region, so ask for one.
      const prompt = regionQuestion(`the ${serviceLabel(check.intent.service)} with ${filterWords(check.intent.filter)}`,
                                    { allRegions: false });
      const args = { text: check.intent.filter.text, match: check.intent.filter.match, services: [check.intent.service] };
      this.set({ status: "idle", clarifying: { tool: "search_resources", args, reason: "tagging request", prompt,
                                               missing: "region", request: check.text, tagIntent: check.intent },
                 discovery: d ? { ...d, stage: "COLLECT_REGION" } : null });
      this.say(prompt, "idle", this.autoListenAfter(null));
      return;
    }
    this.set({ discovery: d ? { ...d, stage: "DISCOVER_RESOURCES" } : null });
    const intent = { ...check.intent, region: check.region };
    if (this.deps.operator?.()) {
      this.set({ tagFlow: null });
      return void this.stepWrite(startWrite(intent, ++this.pendingSeq, check.region));
    }
    return void this.stepTagFlow(startTagFlow(intent, ++this.pendingSeq, check.region));
  }

  /** A typed correction of a misheard request. Only a new request: it can't answer a confirmation or a question in
   *  progress, and a tagging request still needs its spoken "yes, that's right" and, later, a spoken confirmation. */
  correctTranscript(text: string): boolean {
    const t = text.trim();
    const s = this.state;
    if (!t || s.pending || s.status === "executing" || this.isListening() || busyWrite(s.writeFlow)
        || awaitingWrite(s.writeFlow) || awaitingAnswer(s.tagFlow) || s.clarifying) return false;
    this.stopSpeaking(false);
    this.clearAutoTimer();
    this.set({ heardCheck: null, transcript: t, transcriptFinal: true, transcriptConfidence: null, error: null });
    this.planQuestion(t, "Corrected. ");
    return true;
  }

  // ---------- group tagging conversation ----------

  /** The user's answer to "What key and value?" or "Shall I prepare this tagging proposal?". */
  private answerTagFlow(text: string): void {
    void this.stepTagFlow(onAnswer(this.state.tagFlow!, text));
  }

  /** The "Yes" button for one summary: `version` must be the summary on screen, so a stale button does nothing. */
  confirmTagFlow(version: number): void {
    const flow = this.state.tagFlow;
    if (!flow || flow.stage !== "confirm" || flow.version !== version || this.state.status === "executing") return;
    this.abortRecognition();
    void this.stepTagFlow(onAnswer(flow, "yes", version));
  }

  /** Say the step, then run its one read-only call (search or preview) and continue with the result. */
  private async stepTagFlow(step: FlowStep, prefix = ""): Promise<void> {
    const id = step.flow.id;
    this.set({ tagFlow: step.flow, cached: null });
    if (!step.call) {
      if (step.say) this.say(prefix + step.say, "idle", this.autoListenAfter(null));
      return;
    }
    if (!this.deps.call) {
      this.set({ tagFlow: null, status: "idle" });
      this.say(`${prefix}This client can't run the tagging conversation. Nothing was written.`, "idle");
      return;
    }
    this.clearAutoTimer();
    this.stopSpeaking(false);
    this.set({ status: "executing", message: prefix + step.say, lastSpoken: prefix + step.say, error: null,
               lastCall: { tool: step.call.tool, outcome: "running" } });
    const seq = ++this.execSeq;
    let out: { structured?: unknown; errorText?: string };
    try {
      out = await this.deps.call(step.call.tool, step.call.args);
    } catch {
      out = { errorText: "The request failed." };
    }
    const flow = this.state.tagFlow;
    if (seq !== this.execSeq || !flow || flow.id !== id) return; // reset, or replaced by another conversation
    this.set({ lastCall: { tool: step.call.tool, outcome: out.errorText ? "error" : "ok" } });
    const next = step.call.tool === "search_resources"
      ? onSearchResult(flow, out.structured as never, out.errorText)
      : onPreviewResult(flow, out.structured as never, out.errorText);
    this.set({ tagFlow: next.flow, status: "idle" });
    this.say(next.say, "idle", awaitingAnswer(next.flow) ? this.autoListenAfter(null) : undefined);
  }

  /** One step of personal voice tagging: say it, cancel a voided challenge, run its one call, continue. */
  private async stepWrite(step: WriteStep, prefix = ""): Promise<void> {
    const api = this.deps.operator?.() ?? null;
    if (step.cancel && api) void api.cancel(step.cancel);
    this.set({ writeFlow: step.flow, cached: null });
    const say = prefix + step.say;
    if (!step.call) {
      if (say) this.say(say, "idle", awaitingWrite(step.flow) ? this.autoListenAfter(null) : undefined);
      return;
    }
    if (!api || (step.call.kind === "search" && !this.deps.call)) {
      this.set({ writeFlow: null, status: "idle" });
      this.say(`${prefix}Operator voice tagging isn't available right now. Nothing was written.`, "idle");
      return;
    }
    this.clearAutoTimer();
    this.stopSpeaking(false);
    this.set({ status: "executing", message: say, lastSpoken: say, error: null,
               lastCall: { tool: step.call.kind === "search" ? "search_resources" : `voice-tagging ${step.call.kind}`,
                           outcome: "running" } });
    const seq = ++this.execSeq;
    const id = step.flow.id;
    let next: WriteStep;
    if (step.call.kind === "search") {
      const out = await this.deps.call!("search_resources", step.call.args).catch(() => ({ errorText: "failed" }));
      if (seq !== this.execSeq || this.state.writeFlow?.id !== id) return;
      next = onWriteSearch(this.state.writeFlow, (out as { structured?: unknown }).structured as never,
                           (out as { errorText?: string }).errorText);
    } else if (step.call.kind === "prepare") {
      const out = await api.prepare(step.call.spec);
      if (seq !== this.execSeq || this.state.writeFlow?.id !== id) {
        if (out.challenge) void api.cancel(out.challenge.id); // reset or replaced meanwhile: void it at once
        return;
      }
      next = onPrepare(this.state.writeFlow, out);
    } else {
      const out = await api.confirm(step.call.challenge, step.call.transcript, step.call.overwrite);
      if (seq !== this.execSeq || this.state.writeFlow?.id !== id) return;
      next = onConfirm(this.state.writeFlow, out);
    }
    this.set({ status: "idle", lastCall: { tool: this.state.lastCall?.tool ?? "", outcome: "ok" } });
    // What was said before a search or prepare call ("I found 3 ...") leads the next reply; "Applying now" doesn't.
    return this.stepWrite(next, step.call.kind === "prepare" && say ? `${say} ` : "");
  }

  /** "Why is it untagged?" or "What tags should I add?" with no name: the resource the conversation is about, if
   *  there is one (of the right type); otherwise ask which one, listing names from a read-only tool result. */
  private async askWhichResource(plan: Extract<Plan, { kind: "needs" }>, prefix: string): Promise<void> {
    const last = this.state.lastResource;
    const service = plan.args.service as string | undefined;
    if (last && (!service || service === last.service)) {
      const key = plan.tool === "explain_finding" ? "finding_id" : "resource";
      return this.dispatch({ kind: "call", tool: plan.tool, args: { [key]: last.finding_id }, reason: plan.reason },
                           prefix);
    }
    this.set({ status: "idle", clarifying: { tool: plan.tool, args: plan.args, reason: plan.reason,
                                             prompt: plan.prompt, missing: "resource_name" } });
    let names: string[] = [];
    try {
      names = (await this.deps.candidateNames?.(service ? [service] : undefined)) ?? [];
    } catch {
      names = [];
    }
    if (!this.state.clarifying) return; // answered or cancelled meanwhile
    const options = names.length ? ` For example: ${sayNames(names)}.` : "";
    this.say(prefix + plan.prompt + options, "idle", this.autoListenAfter(null));
  }

  /** A matched call: answered from the cache, run at once (read-only), or held for confirmation. */
  private dispatch(plan: Extract<Plan, { kind: "call" }>, prefix = "", request?: string): void {
    // A listing whose region is known: DISCOVER_RESOURCES in a new context (new id). Anything else ends discovery.
    const listing = isListing(plan.tool);
    const ctx = listing ? discoveryFor(++this.pendingSeq, request ?? this.state.transcript, plan.tool, plan.args,
                                       "DISCOVER_RESOURCES") : null;
    this.set({ discovery: ctx });
    // Already answered recently: reuse it. Reading the cache runs no tool; Refresh fetches fresh data.
    const hit = this.deps.lookup?.(plan.tool, plan.args) ?? null;
    if (hit) {
      if (ctx) this.set({ discovery: { ...ctx, stage: "DISPLAY_RESULTS" } });
      const age = ageWords(hit.ageMs);
      this.set({ status: "idle", lastCall: { tool: plan.tool, outcome: "cached" },
                 cached: { tool: plan.tool, args: plan.args, age }, ...(hit.resource ? { lastResource: hit.resource } : {}) });
      this.sayAnswer(`${prefix}${CACHED_INTRO(age)} ${hit.speech}`);
      return;
    }
    if (!this.needsConfirmation(plan.tool)) {
      if (prefix) this.set({ message: prefix.trim() }); // "Searching Lambda functions in ..." while it runs
      void this.run({ tool: plan.tool, args: plan.args }, prefix);
      return;
    }
    const id = ++this.pendingSeq;
    const prompt = `I can ${describeCall(plan.tool, plan.args)}. Shall I continue?`;
    this.set({ status: "awaiting_confirmation", pending: { id, tool: plan.tool, args: plan.args, prompt, note: plan.note } });
    this.say(prefix + prompt, "awaiting_confirmation", this.autoListenAfter(id));
  }

  /** Run the pending call, once. `id` must match the pending call, so a stale button or answer can't run another. */
  async confirm(id: number): Promise<void> {
    const pending = this.state.pending;
    if (!pending || pending.id !== id || this.state.status === "executing") return;
    this.abortRecognition(); // a button press while listening: whatever is still being heard must not act again
    this.set({ pending: null });
    await this.run(pending);
  }

  /** Call the tool once and speak the answer. Reached only via confirm() or for a read-only tool. */
  private async run(pending: { tool: string; args: Record<string, unknown>; refresh?: boolean }, prefix = ""): Promise<void> {
    this.clearAutoTimer();
    this.stopSpeaking(false);
    this.set({ status: "executing", error: null, interim: "", lastCall: { tool: pending.tool, outcome: "running" } });
    const seq = ++this.execSeq;
    let result: ExecuteResult;
    try {
      result = await (pending.refresh ? this.deps.execute(pending.tool, pending.args, { refresh: true })
        : this.deps.execute(pending.tool, pending.args));
    } catch {
      result = { errorText: "The request failed." };
    }
    if (seq !== this.execSeq) return; // reset, or replaced by a newer call (another region): never shown
    const d = this.state.discovery;
    if (d?.stage === "DISCOVER_RESOURCES" && d.tool === pending.tool) this.set({ discovery: { ...d, stage: "DISPLAY_RESULTS" } });
    if (result.speech && !result.isError) {
      this.set({ lastCall: { tool: pending.tool, outcome: "ok" }, cached: null,
                 ...(result.resource ? { lastResource: result.resource } : {}) });
      this.sayAnswer(`${prefix}${result.speech}`);
    } else {
      const text = result.errorText ?? "The server returned no spoken answer.";
      this.set({ status: "error", error: text, lastCall: { tool: pending.tool, outcome: "error" } });
      this.say(`Sorry, that didn't work. ${text}`, "error");
    }
  }

  /** Speak an answer (fresh or cached), then continue the hands-free conversation. Replay repeats the answer. */
  private sayAnswer(text: string): void {
    this.say(text, "idle", () => {
      if (this.state.autoListen) this.speakNow(FOLLOW_UP_TEXT, "idle", this.autoListenAfter(null));
    });
  }

  /** Fetch fresh data for the answer that was reused from the cache. Asks first only for a non-read-only tool. */
  refresh(): void {
    const c = this.state.cached;
    if (!c || this.state.pending || this.state.status === "executing" || this.isListening()) return;
    if (!this.needsConfirmation(c.tool)) {
      this.set({ cached: null });
      void this.run({ tool: c.tool, args: c.args, refresh: true });
      return;
    }
    const id = ++this.pendingSeq;
    const prompt = `I can fetch fresh data to ${describeCall(c.tool, c.args)}. Shall I continue?`;
    this.stopSpeaking(false);
    this.set({ status: "awaiting_confirmation", cached: null,
               pending: { id, tool: c.tool, args: c.args, prompt, refresh: true } });
    this.say(prompt, "awaiting_confirmation", this.autoListenAfter(id));
  }

  cancel(): void {
    if (!this.state.pending) return;
    this.abortRecognition();
    this.clearAutoTimer();
    this.set({ pending: null, status: "idle", interim: "", error: null });
    this.say(CANCELLED_TEXT, "idle", this.autoListenAfter(null));
  }

  // ---------- speaking ----------

  private say(text: string, after: VoiceStatus, then?: () => void): void {
    this.set({ message: text, lastSpoken: text });
    this.speakNow(text, after, then);
  }

  /** `then` runs only when this utterance finishes on its own (or at once when muted or unsupported), never when it
   *  is cut off by stop, replay, listening or a newer utterance. */
  private speakNow(text: string, after: VoiceStatus, then?: () => void): void {
    const { synth, createUtterance } = this.deps;
    this.cancelSynth(); // never overlap: the previous utterance stops first
    const seq = ++this.utteranceSeq;
    if (this.state.muted || !synth || !createUtterance || !text) {
      this.set({ status: after });
      then?.();
      return;
    }
    // Chrome and Edge often list no voices until after the page loads. If none were known yet, look again now, so the
    // first reply uses the selected voice rather than the browser default (a different voice, often louder).
    if (this.voices.length === 0) this.loadVoices();
    const u = createUtterance(text);
    u.lang = this.voice?.lang ?? "en-US";
    if (this.voice) u.voice = this.voice;
    u.volume = SPEECH_SETTINGS.volume;
    u.rate = SPEECH_SETTINGS.rate;
    u.pitch = SPEECH_SETTINGS.pitch;
    this.current = u;
    let finished = false;
    const done = () => {
      if (finished) return; // the end event and the fallback can both report the same end
      finished = true;
      this.clearSpeechTimer();
      if (this.current === u) this.current = null;
      if (seq !== this.utteranceSeq || this.state.status !== "speaking") return;
      this.set({ status: after });
      then?.();
    };
    u.onend = done;
    u.onerror = done;
    // Fallback for engines that end an utterance without firing onend (seen with Chrome and Edge online voices):
    // once the engine has been seen speaking and then reports silence twice in a row, treat the utterance as ended.
    // An engine that reports nothing at all is given speechMaxMs(text).
    let started = false;
    let quiet = 0;
    let waited = 0;
    u.onstart = () => { started = true; };
    const poll = () => {
      this.speechTimer = null;
      if (finished || seq !== this.utteranceSeq) return;
      waited += SPEECH_POLL_MS;
      if (typeof synth.speaking === "boolean") {
        if (synth.speaking || synth.pending) { started = true; quiet = 0; } else if (started && ++quiet >= 2) return done();
      }
      if (waited >= speechMaxMs(text)) return done();
      this.speechTimer = this.timers.set(poll, SPEECH_POLL_MS);
    };
    this.set({ status: "speaking" });
    synth.speak(u);
    this.clearSpeechTimer();
    this.speechTimer = this.timers.set(poll, SPEECH_POLL_MS);
  }

  private clearSpeechTimer(): void {
    if (this.speechTimer !== null) this.timers.clear(this.speechTimer);
    this.speechTimer = null;
  }

  /** Cancel queued or playing speech. Skipped when the engine reports itself idle, so an idle engine is never reset
   *  between utterances. Engines that don't report speaking/pending are always cancelled. */
  private cancelSynth(): void {
    const synth = this.deps.synth;
    const unfinished = this.current !== null; // an utterance whose end we haven't seen may still be playing
    this.current = null;
    this.clearSpeechTimer();
    if (!synth) return;
    const idle = synth.speaking === false && synth.pending === false;
    if (!idle || unfinished) synth.cancel();
  }

  replay(): void {
    if (!this.state.lastSpoken || this.state.muted || this.state.status === "executing" || this.isListening()) return;
    this.clearAutoTimer();
    this.speakNow(this.state.lastSpoken, this.state.pending ? "awaiting_confirmation" : "idle");
  }

  stopSpeaking(updateStatus = true): void {
    this.utteranceSeq++;
    this.cancelSynth();
    if (updateStatus && this.state.status === "speaking") {
      this.set({ status: this.state.pending ? "awaiting_confirmation" : "idle" });
    }
  }

  setMuted(muted: boolean): void {
    if (muted) this.stopSpeaking();
    this.set({ muted });
  }

  /** Forget everything (disconnect or unmount): no listening, no pending call, nothing scheduled. */
  reset(): void {
    // Disconnect: a pending voice-tagging confirmation is cancelled on the server too (its session would void it
    // anyway on logout or expiry).
    const challenge = this.state.writeFlow?.challenge;
    if (challenge) void this.deps.operator?.()?.cancel(challenge);
    this.execSeq++;
    this.clearAutoTimer();
    this.abortRecognition();
    this.stopSpeaking(false);
    this.set({ status: "idle", interim: "", transcript: "", transcriptFinal: true, transcriptConfidence: null, heardCheck: null,
               pending: null, message: "", lastSpoken: "", error: null, lastCall: null, listenMode: null, cached: null, clarifying: null, lastResource: null, tagFlow: null, writeFlow: null, discovery: null });
    this.clearSpeechTimer();
  }
}

export function recognitionErrorMessage(code: string): string {
  switch (code) {
    case "not-allowed":
    case "service-not-allowed":
      return "Microphone access was denied. Allow the microphone for this page in the browser, or type your question.";
    case "no-speech":
      return "I didn't hear anything. Try again.";
    case "audio-capture":
      return "No microphone was found.";
    case "network":
      return "The browser's speech service could not be reached. Type your question instead.";
    case "aborted":
      return "Listening was stopped.";
    case "language-not-supported":
      return "This browser can't recognize English (US) speech.";
    default:
      return "Voice input failed. Try again, or type your question.";
  }
}

/** English (India) when the browser or the time zone says India, else the browser's own English, else US English.
 *  Read once at startup; the panel's selector changes it. Nothing is stored. */
export function defaultRecognitionLang(
  languages: readonly string[] = typeof navigator !== "undefined" ? navigator.languages ?? [] : [],
  timeZone: string = Intl.DateTimeFormat().resolvedOptions().timeZone ?? "",
): string {
  if (languages.some((l) => /^en-IN$/i.test(l)) || /^Asia\/(Kolkata|Calcutta)$/.test(timeZone)) return "en-IN";
  const en = languages.find((l) => /^en-(US|GB)$/i.test(l));
  return en ? `en-${en.slice(3).toUpperCase()}` : "en-US";
}

/** The real browser APIs, or nulls where unsupported. */
export function browserVoiceDeps(): Pick<VoiceDeps, "createRecognition" | "synth" | "createUtterance"> {
  const w = window as unknown as {
    SpeechRecognition?: new () => RecognitionLike;
    webkitSpeechRecognition?: new () => RecognitionLike;
    speechSynthesis?: SynthLike;
    SpeechSynthesisUtterance?: new (text: string) => UtteranceLike;
  };
  const Ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
  const Utterance = w.SpeechSynthesisUtterance;
  return {
    createRecognition: Ctor ? () => new Ctor() : null,
    synth: w.speechSynthesis ?? null,
    createUtterance: Utterance ? (text: string) => new Utterance(text) : null,
  };
}
