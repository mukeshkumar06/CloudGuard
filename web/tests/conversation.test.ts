// Hands-free conversation: automatic listening after prompts and answers, the listening timeout, retry, and the
// safety rules (one recognition session, answers bound to their call, no call without its own yes). Fake speech
// APIs and fake timers; no browser, no network.
import { describe, expect, it, vi } from "vitest";

import {
  AUTO_LISTEN_DELAY_MS, CONFIRM_TIMEOUT_TEXT, FOLLOW_UP_TEXT, LISTEN_TIMEOUT_MS, QUESTION_TIMEOUT_TEXT, REPEAT_TEXT,
  VoiceController, type ExecuteResult, type RecognitionLike, type SynthLike, type Timers, type UtteranceLike,
  type VoiceDeps,
} from "../src/lib/voice";

const SPEECH = "Your AWS bill for September 2026 was 12 dollars and 34 cents.";
const PROMPT = "I can check your AWS bill for last month. Shall I continue?";

class Rec implements RecognitionLike {
  lang = ""; interimResults = false; continuous = true; maxAlternatives = 0;
  onresult: RecognitionLike["onresult"] = null;
  onerror: RecognitionLike["onerror"] = null;
  onend: RecognitionLike["onend"] = null;
  started = 0; aborted = 0;
  onstart: RecognitionLike["onstart"] = null;
  start() { this.started++; this.onstart?.(); }
  stop() { this.onend?.(); }
  abort() { this.aborted++; this.onerror?.({ error: "aborted" }); this.onend?.(); }
  hear(text: string) {
    this.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: true })] });
    this.onend?.();
  }
  fail(code: string) { this.onerror?.({ error: code }); this.onend?.(); }
}

class Synth implements SynthLike {
  spoken: UtteranceLike[] = [];
  active: UtteranceLike | null = null;
  overlaps = 0;
  speak(u: UtteranceLike) { if (this.active) this.overlaps++; this.active = u; this.spoken.push(u); }
  cancel() { const u = this.active; this.active = null; u?.onerror?.(); }
  /** The current utterance finishes on its own. */
  finish() { const u = this.active; this.active = null; u?.onend?.(); }
  get texts() { return this.spoken.map((u) => u.text); }
}

class FakeTimers implements Timers {
  private now = 0;
  private seq = 0;
  private due = new Map<number, { at: number; fn: () => void }>();
  set(fn: () => void, ms: number) { const id = ++this.seq; this.due.set(id, { at: this.now + ms, fn }); return id; }
  clear(h: unknown) { this.due.delete(h as number); }
  advance(ms: number) {
    const end = this.now + ms;
    for (;;) {
      const next = [...this.due.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      this.due.delete(next[0]);
      this.now = next[1].at;
      next[1].fn();
    }
    this.now = end;
  }
  get size() { return this.due.size; }
}

function setup(opts: { autoListen?: boolean; muted?: boolean; result?: () => Promise<ExecuteResult> } = {}) {
  const recs: Rec[] = [];
  const synth = new Synth();
  const timers = new FakeTimers();
  const execute = vi.fn<VoiceDeps["execute"]>(opts.result ?? (async () => ({ speech: SPEECH })));
  const c = new VoiceController({
    createRecognition: () => { const r = new Rec(); recs.push(r); return r; },
    synth,
    createUtterance: (text) => ({ text, lang: "", onend: null, onerror: null }),
    tools: () => ["get_monthly_aws_bill", "scan_tag_compliance"],
    execute,
    timers,
  }, undefined, { autoListen: opts.autoListen ?? true });
  if (opts.muted) c.setMuted(true);
  const flush = () => new Promise((r) => setTimeout(r, 0));
  const rec = () => recs[recs.length - 1];
  /** CloudGuard finishes speaking, then the auto-listen gap passes. */
  const afterSpeech = () => { synth.finish(); timers.advance(AUTO_LISTEN_DELAY_MS); };
  /** Ask a question by tapping the microphone, and let the prompt finish. */
  const askByMic = async (q: string) => { c.startListening(); rec().hear(q); await flush(); };
  return { c, recs, rec, synth, timers, execute, flush, afterSpeech, askByMic };
}

describe("automatic confirmation listening", () => {
  it("starts listening for confirmation after the prompt finishes speaking, not before", async () => {
    const { c, recs, synth, timers, askByMic } = setup();
    await askByMic("How much did AWS cost last month?");
    expect(synth.texts).toEqual([PROMPT]);
    expect(c.snapshot.status).toBe("speaking");
    timers.advance(10_000);
    expect(recs).toHaveLength(1); // not while the prompt is still being spoken
    synth.finish();
    expect(c.snapshot.status).toBe("awaiting_confirmation");
    expect(recs).toHaveLength(1); // a short gap first, so it doesn't hear its own echo
    timers.advance(AUTO_LISTEN_DELAY_MS);
    expect(recs).toHaveLength(2);
    expect(c.snapshot).toMatchObject({ status: "listening", listenMode: "confirmation" });
  });

  it("spoken yes executes exactly once, then speaks the server speech", async () => {
    const { c, rec, synth, execute, afterSpeech, askByMic, flush } = setup();
    await askByMic("How much did AWS cost last month?");
    afterSpeech();
    rec().hear("yes");
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith("get_monthly_aws_bill", { month: "last-month" });
    expect(synth.texts.at(-1)).toBe(SPEECH);
    expect(c.snapshot).toMatchObject({ message: SPEECH, lastSpoken: SPEECH, pending: null });
    rec().hear("yes"); // a late duplicate from the ended session
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("spoken no executes nothing", async () => {
    const { c, rec, synth, execute, afterSpeech, askByMic, flush } = setup();
    await askByMic("How much did AWS cost last month?");
    afterSpeech();
    rec().hear("no");
    await flush();
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.pending).toBeNull();
    expect(synth.texts.at(-1)).toBe("Okay, cancelled. Nothing was run.");
  });

  it("an ambiguous answer keeps the call pending, asks again and listens again", async () => {
    const { c, recs, rec, synth, execute, afterSpeech, askByMic, flush } = setup();
    await askByMic("How much did AWS cost last month?");
    afterSpeech();
    const pending = c.snapshot.pending;
    rec().hear("maybe later");
    await flush();
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.pending).toBe(pending);
    expect(synth.texts.at(-1)).toBe(`${REPEAT_TEXT} ${PROMPT}`);
    const before = recs.length;
    afterSpeech();
    expect(recs.length).toBe(before + 1);
    expect(c.snapshot.listenMode).toBe("confirmation");
    rec().hear("go ahead");
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("muted: still listens for the answer after showing the prompt", async () => {
    const { c, recs, synth, timers, askByMic } = setup({ muted: true });
    await askByMic("How much did AWS cost last month?");
    expect(synth.spoken).toHaveLength(0);
    expect(c.snapshot.message).toBe(PROMPT);
    timers.advance(AUTO_LISTEN_DELAY_MS);
    expect(recs).toHaveLength(2);
  });
});

describe("timeout and retry", () => {
  it("stops after the timeout with a clear message; retry listens again for the same call", async () => {
    const { c, recs, rec, execute, timers, afterSpeech, askByMic, flush } = setup();
    await askByMic("How much did AWS cost last month?");
    afterSpeech();
    const session = rec();
    timers.advance(LISTEN_TIMEOUT_MS - 1);
    expect(c.snapshot.status).toBe("listening");
    timers.advance(1);
    expect(session.aborted).toBe(1);
    expect(c.snapshot).toMatchObject({ status: "awaiting_confirmation", error: CONFIRM_TIMEOUT_TEXT, listenMode: null });
    expect(c.snapshot.pending).not.toBeNull();
    timers.advance(60_000);
    expect(recs).toHaveLength(2); // no automatic retry loop
    session.hear("yes"); // a late result from the timed-out session
    await flush();
    expect(execute).not.toHaveBeenCalled();
    c.startListening(); // the Try again control
    expect(c.snapshot).toMatchObject({ status: "listening", listenMode: "confirmation", error: null });
    rec().hear("yes");
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("question listening times out too", () => {
    const { c, timers } = setup();
    c.startListening();
    timers.advance(LISTEN_TIMEOUT_MS);
    expect(c.snapshot).toMatchObject({ status: "idle", error: QUESTION_TIMEOUT_TEXT });
  });

  it("an answer before the timeout clears the timer", async () => {
    const { timers, askByMic, afterSpeech } = setup({ autoListen: false });
    await askByMic("Check my tagging compliance.");
    afterSpeech();
    expect(timers.size).toBe(0);
  });

  it("permission denied stops listening, keeps the call pending and does not retry by itself", async () => {
    const { c, recs, rec, execute, timers, afterSpeech, askByMic } = setup();
    await askByMic("How much did AWS cost last month?");
    afterSpeech();
    rec().fail("not-allowed");
    expect(c.snapshot.status).toBe("error");
    expect(c.snapshot.error).toMatch(/Microphone access was denied/);
    expect(c.snapshot.pending).not.toBeNull();
    timers.advance(60_000);
    expect(recs).toHaveLength(2);
    expect(execute).not.toHaveBeenCalled();
  });
});

describe("continuous conversation", () => {
  it("auto-listen on: asks a follow-up after the answer and listens for the next question", async () => {
    const { c, recs, rec, synth, execute, afterSpeech, askByMic, flush } = setup();
    await askByMic("How much did AWS cost last month?");
    afterSpeech();
    rec().hear("yes");
    await flush();
    synth.finish(); // the server speech ends
    expect(synth.texts.at(-1)).toBe(FOLLOW_UP_TEXT);
    expect(c.snapshot.lastSpoken).toBe(SPEECH); // Replay repeats the answer, not the follow-up
    const before = recs.length;
    afterSpeech();
    expect(recs.length).toBe(before + 1);
    expect(c.snapshot).toMatchObject({ status: "listening", listenMode: "question" });
    // The next question gets its own confirmation; nothing runs automatically.
    rec().hear("Check my tagging compliance.");
    await flush();
    expect(c.snapshot.pending).toMatchObject({ tool: "scan_tag_compliance" });
    expect(execute).toHaveBeenCalledTimes(1);
    afterSpeech();
    rec().hear("yes");
    await flush();
    expect(execute).toHaveBeenCalledTimes(2);
    expect(execute).toHaveBeenLastCalledWith("scan_tag_compliance", {});
  });

  it("auto-listen off: no automatic listening and no follow-up; the result returns to idle", async () => {
    const { c, recs, synth, timers, askByMic, flush } = setup({ autoListen: false });
    await askByMic("How much did AWS cost last month?");
    synth.finish();
    timers.advance(10_000);
    expect(recs).toHaveLength(1);
    expect(c.snapshot.status).toBe("awaiting_confirmation");
    await c.confirm(c.snapshot.pending!.id);
    await flush();
    synth.finish();
    timers.advance(10_000);
    expect(synth.texts).toEqual([PROMPT, SPEECH]);
    expect(recs).toHaveLength(1);
    expect(c.snapshot.status).toBe("idle");
  });

  it("turning auto-listen off during the gap cancels the scheduled listening", async () => {
    const { c, recs, synth, timers, askByMic } = setup();
    await askByMic("How much did AWS cost last month?");
    synth.finish();
    c.setAutoListen(false);
    timers.advance(AUTO_LISTEN_DELAY_MS);
    expect(recs).toHaveLength(1);
  });

  it("stopping speech, replay or preview does not trigger auto-listening", async () => {
    const { c, recs, timers, askByMic } = setup();
    await askByMic("How much did AWS cost last month?");
    c.stopSpeaking();
    timers.advance(10_000);
    expect(recs).toHaveLength(1);
    c.replay();
    c.preview();
    timers.advance(10_000);
    expect(recs).toHaveLength(1);
  });

  it("a tool error is spoken and does not auto-listen", async () => {
    const { c, recs, rec, synth, afterSpeech, askByMic, flush } = setup({ result: async () => ({ errorText: "Server down." }) });
    await askByMic("How much did AWS cost last month?");
    afterSpeech();
    rec().hear("yes");
    await flush();
    const before = recs.length;
    afterSpeech();
    expect(recs.length).toBe(before);
    expect(c.snapshot.status).toBe("error");
    expect(synth.texts.at(-1)).toBe("Sorry, that didn't work. Server down.");
  });
});

describe("safety and state", () => {
  it("never runs two recognition sessions", async () => {
    const { c, recs, synth, timers, askByMic } = setup();
    await askByMic("How much did AWS cost last month?");
    synth.finish();
    c.startListening(); // manual tap during the auto-listen gap
    timers.advance(AUTO_LISTEN_DELAY_MS); // the scheduled start finds a session already running
    c.startListening();
    expect(recs).toHaveLength(2);
    expect(recs.filter((r) => r.started === 1 && r.aborted === 0)).toHaveLength(2); // the first ended normally
  });

  it("disconnect while listening aborts the session; its late events do nothing", async () => {
    const { c, recs, rec, execute, timers, afterSpeech, askByMic, flush } = setup();
    await askByMic("How much did AWS cost last month?");
    afterSpeech();
    const session = rec();
    c.reset();
    expect(session.aborted).toBe(1);
    expect(c.snapshot).toMatchObject({ status: "idle", pending: null, listenMode: null, error: null });
    session.hear("yes");
    await flush();
    timers.advance(60_000);
    expect(execute).not.toHaveBeenCalled();
    expect(recs).toHaveLength(2);
    expect(timers.size).toBe(0);
  });

  it("disconnect during the auto-listen gap cancels it", async () => {
    const { c, recs, synth, timers, askByMic } = setup();
    await askByMic("How much did AWS cost last month?");
    synth.finish();
    c.reset();
    timers.advance(AUTO_LISTEN_DELAY_MS);
    expect(recs).toHaveLength(1);
  });

  it("a yes heard for a call that was cancelled by button meanwhile does nothing", async () => {
    const { c, rec, execute, afterSpeech, askByMic, flush } = setup();
    await askByMic("How much did AWS cost last month?");
    afterSpeech();
    const session = rec();
    c.cancel(); // aborts the session
    session.hear("yes");
    await flush();
    expect(execute).not.toHaveBeenCalled();
  });

  it("a confirmation answer cannot reach a newer pending call", async () => {
    const { c, rec, execute, afterSpeech, askByMic, flush } = setup();
    await askByMic("How much did AWS cost last month?");
    afterSpeech();
    const session = rec();
    // Simulate the pending call being replaced while this session is still open (bypassing the UI).
    const internal = c as unknown as { state: { pending: { id: number } | null } };
    internal.state = { ...internal.state, pending: { ...internal.state.pending!, id: 999 } };
    session.hear("yes");
    await flush();
    expect(execute).not.toHaveBeenCalled();
  });

  it("button confirm while auto-listening runs exactly once; the open session is dropped", async () => {
    const { c, rec, execute, afterSpeech, askByMic, flush } = setup();
    await askByMic("How much did AWS cost last month?");
    afterSpeech();
    const session = rec();
    await c.confirm(c.snapshot.pending!.id);
    session.hear("yes");
    await flush();
    expect(session.aborted).toBe(1);
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("no speech overlaps across the whole conversation", async () => {
    const { rec, synth, afterSpeech, askByMic, flush } = setup();
    await askByMic("How much did AWS cost last month?");
    afterSpeech();
    rec().hear("yes");
    await flush();
    afterSpeech();
    afterSpeech();
    expect(synth.overlaps).toBe(0);
  });
});
