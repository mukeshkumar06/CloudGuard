// Regression tests for hands-free listening reliability: SPEAKING -> (gap) -> STARTING -> LISTENING on onstart,
// graceful timeouts that keep late words, Chrome ending sessions by itself, and no duplicate sessions or calls.
// Fake Web Speech APIs and fake timers; no browser, no microphone, no network.
import { describe, expect, it, vi } from "vitest";

import {
  AUTO_LISTEN_DELAY_MS, CONFIRM_TIMEOUT_TEXT, LISTEN_TIMEOUT_MS, SPEECH_POLL_MS, START_TIMEOUT_MS, STOP_GRACE_MS,
  PARTIAL_TEXT, VoiceController, defaultRecognitionLang, speechMaxMs, type ExecuteResult, type RecognitionLike, type SynthLike,
  type Timers, type UtteranceLike, type VoiceDeps,
} from "../src/lib/voice";

const PROMPT = "I can check which AWS account CloudGuard is using. Shall I continue?";

/** A recognizer whose onstart is fired by the test (`open()`), like a browser that takes time to open the mic. */
class Rec implements RecognitionLike {
  lang = ""; interimResults = false; continuous = true; maxAlternatives = 0;
  onresult: RecognitionLike["onresult"] = null;
  onerror: RecognitionLike["onerror"] = null;
  onend: RecognitionLike["onend"] = null;
  onstart: RecognitionLike["onstart"] = null;
  onspeechstart: RecognitionLike["onspeechstart"] = null;
  started = 0; stopped = 0; aborted = 0;
  constructor(private throwOnStart = false) {}
  start() { if (this.throwOnStart) throw new DOMException("already started", "InvalidStateError"); this.started++; }
  stop() { this.stopped++; }
  abort() { this.aborted++; this.onerror?.({ error: "aborted" }); this.onend?.(); }
  open() { this.onstart?.(); }
  speech() { this.onspeechstart?.(); }
  interim(t: string) { this.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript: t }], { isFinal: false })] }); }
  final(t: string) { this.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript: t }], { isFinal: true })] }); }
  end() { this.onend?.(); }
  error(code: string) { this.onerror?.({ error: code }); }
}

/** A synthesizer that reports `speaking`, like Chrome. `finish(false)` ends audio without firing onend. */
class Synth implements SynthLike {
  spoken: UtteranceLike[] = [];
  active: UtteranceLike | null = null;
  speaking = false;
  pending = false;
  overlaps = 0;
  speak(u: UtteranceLike) { if (this.active) this.overlaps++; this.active = u; this.spoken.push(u); this.speaking = true; u.onstart?.(); }
  cancel() { const u = this.active; this.active = null; this.speaking = false; u?.onerror?.(); }
  finish(fireEnd = true) { const u = this.active; this.active = null; this.speaking = false; if (fireEnd) u?.onend?.(); }
}

class FakeTimers implements Timers {
  now = 0;
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
}

function setup(opts: { throwOnStart?: boolean; result?: () => Promise<ExecuteResult> } = {}) {
  const recs: Rec[] = [];
  const synth = new Synth();
  const timers = new FakeTimers();
  const execute = vi.fn<VoiceDeps["execute"]>(opts.result ?? (async () => ({ speech: "You are connected to account 9012." })));
  const c = new VoiceController({
    createRecognition: () => { const r = new Rec(opts.throwOnStart); recs.push(r); return r; },
    synth, createUtterance: (text) => ({ text, lang: "", onend: null, onerror: null }),
    tools: () => ["get_aws_account_identity"], execute, timers,
  }, undefined, { autoListen: true });
  const rec = () => recs[recs.length - 1];
  const flush = () => new Promise((r) => setTimeout(r, 0));
  /** Ask by mic and get to the point where the confirmation prompt is being spoken. */
  const ask = async () => {
    c.startListening(); rec().open(); rec().final("Which AWS account am I using?"); rec().end();
    await flush();
    expect(synth.active?.text).toBe(PROMPT);
  };
  /** The prompt ends and the auto-listen gap passes: a confirmation session is requested. */
  const promptEnds = () => { synth.finish(); timers.advance(AUTO_LISTEN_DELAY_MS); };
  return { c, recs, rec, synth, timers, execute, flush, ask, promptEnds };
}

describe("1-3: speaking -> starting -> listening", () => {
  it("1. confirmation speech ends and listening starts automatically", async () => {
    const { c, recs, synth, timers, ask } = setup();
    await ask();
    expect(c.snapshot.status).toBe("speaking");
    synth.finish();
    expect(c.snapshot.status).toBe("awaiting_confirmation");
    timers.advance(AUTO_LISTEN_DELAY_MS - 1);
    expect(recs).toHaveLength(1);
    timers.advance(1);
    expect(recs).toHaveLength(2);
    expect(recs[1].started).toBe(1);
  });

  it("2. the panel says listening only after recognition.onstart", async () => {
    const { c, rec, ask, promptEnds } = setup();
    await ask();
    promptEnds();
    expect(c.snapshot).toMatchObject({ status: "starting", listenMode: "confirmation" });
    rec().open();
    expect(c.snapshot).toMatchObject({ status: "listening", listenMode: "confirmation" });
  });

  it("3. a delayed onstart: the listening timeout counts from onstart, and a never-opened mic gives a retry", async () => {
    const { c, rec, timers, ask, promptEnds } = setup();
    await ask();
    promptEnds();
    timers.advance(START_TIMEOUT_MS - 500); // the browser is slow to open the microphone
    expect(c.snapshot.status).toBe("starting");
    rec().open();
    timers.advance(LISTEN_TIMEOUT_MS - 1); // a full listening window from onstart, not from start()
    expect(c.snapshot.status).toBe("listening");

    // Separately: a microphone that never opens.
    const s2 = setup();
    await s2.ask();
    s2.promptEnds();
    s2.timers.advance(START_TIMEOUT_MS);
    expect(s2.c.snapshot.status).toBe("error");
    expect(s2.c.snapshot.error).toMatch(/microphone didn't start/);
    expect(s2.rec().aborted).toBe(1);
    expect(s2.c.snapshot.pending).not.toBeNull();
    expect(s2.execute).not.toHaveBeenCalled();
  });

  it("the end of speech is detected even when the engine never fires onend", async () => {
    const { c, recs, synth, timers, ask } = setup();
    await ask();
    synth.finish(false); // audio stopped, no onend (seen with Chrome/Edge online voices)
    timers.advance(SPEECH_POLL_MS * 2);
    expect(c.snapshot.status).toBe("awaiting_confirmation");
    timers.advance(AUTO_LISTEN_DELAY_MS);
    expect(recs).toHaveLength(2);
  });

  it("an engine that reports nothing at all is given a length-based maximum", async () => {
    const recs: Rec[] = [];
    const timers = new FakeTimers();
    const silent: SynthLike = { speak() {}, cancel() {} }; // no speaking flag, no events
    const c = new VoiceController({ createRecognition: () => { const r = new Rec(); recs.push(r); return r; },
      synth: silent, createUtterance: (text) => ({ text, lang: "", onend: null, onerror: null }),
      tools: () => ["get_aws_account_identity"], execute: async () => ({}), timers });
    c.startListening(); recs[0].open(); recs[0].final("Which AWS account am I using?"); recs[0].end();
    await new Promise((r) => setTimeout(r, 0));
    timers.advance(speechMaxMs(PROMPT) - SPEECH_POLL_MS);
    expect(c.snapshot.status).toBe("speaking");
    timers.advance(SPEECH_POLL_MS * 2 + AUTO_LISTEN_DELAY_MS);
    expect(recs).toHaveLength(2);
  });

  it("does not start listening while the engine is still speaking (no early fallback)", async () => {
    const { recs, synth, timers, ask } = setup();
    await ask();
    timers.advance(5000);
    expect(synth.speaking).toBe(true);
    expect(recs).toHaveLength(1);
  });
});

describe("4-5: start failures", () => {
  it("4. recognition.start() throwing gives an error with retry, keeps the call, runs nothing", async () => {
    const { c, ask, promptEnds, execute } = setup();
    await ask();
    // Make the next session's start() throw, like Chrome's InvalidStateError.
    const internal = c as unknown as { deps: VoiceDeps };
    const real = internal.deps.createRecognition!;
    internal.deps.createRecognition = () => { const r = real() as Rec; r.start = () => { throw new DOMException("busy", "InvalidStateError"); }; return r; };
    promptEnds();
    expect(c.snapshot.status).toBe("error");
    expect(c.snapshot.error).toMatch(/microphone is busy/);
    expect(c.snapshot.pending).not.toBeNull();
    internal.deps.createRecognition = real;
    c.startListening(); // Try again
    expect(c.snapshot.status).toBe("starting");
    expect(execute).not.toHaveBeenCalled();
  });

  it("5. microphone permission denied", async () => {
    const { c, rec, recs, timers, ask, promptEnds, execute } = setup();
    await ask();
    promptEnds();
    rec().error("not-allowed");
    rec().end();
    expect(c.snapshot.status).toBe("error");
    expect(c.snapshot.error).toMatch(/Microphone access was denied/);
    expect(c.snapshot.pending).not.toBeNull();
    timers.advance(60_000);
    expect(recs).toHaveLength(2); // no automatic retry
    expect(execute).not.toHaveBeenCalled();
  });
});

describe("6-9: hearing the answer", () => {
  it("6. interim result followed by a final YES executes once", async () => {
    const { c, rec, ask, promptEnds, flush, execute } = setup();
    await ask();
    promptEnds();
    rec().open();
    rec().speech();
    rec().interim("ye");
    expect(c.snapshot.interim).toBe("ye");
    rec().final("yes");
    rec().end();
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith("get_aws_account_identity", {});
  });

  it("7. a YES still being spoken at the timeout is wrapped up gracefully, not aborted, and executes once", async () => {
    const { rec, timers, ask, promptEnds, flush, execute } = setup();
    await ask();
    promptEnds();
    const session = rec();
    session.open();
    timers.advance(LISTEN_TIMEOUT_MS - 100);
    session.interim("yes"); // words arrive just before the timeout
    timers.advance(100);
    expect(session.aborted).toBe(0);
    expect(session.stopped).toBe(1); // stop() lets the recognizer finalise
    session.final("yes");
    session.end();
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("7b. if the recognizer never finalises after stop(), the interim YES confirms nothing and the call stays pending", async () => {
    const { c, rec, timers, ask, promptEnds, flush, execute } = setup();
    await ask();
    promptEnds();
    const session = rec();
    session.open();
    session.speech();
    session.interim("yes");
    timers.advance(LISTEN_TIMEOUT_MS * 3); // speaking cap, then the stop grace period
    await flush();
    expect(session.stopped).toBe(1);
    expect(session.aborted).toBe(1);
    expect(execute).not.toHaveBeenCalled(); // interim words are never a confirmation
    expect(c.snapshot.status).toBe("awaiting_confirmation");
    expect(c.snapshot.message).toContain(PARTIAL_TEXT);
    session.final("yes"); // late events from the ended session
    session.end();
    await flush();
    expect(execute).not.toHaveBeenCalled();
  });

  it("8. Chrome ending the session by itself: interim words are not a confirmation; without words, a retry is offered", async () => {
    const { c, rec, ask, promptEnds, flush, execute } = setup();
    await ask();
    promptEnds();
    rec().open();
    rec().interim("yes"); // Chrome ends without a final result
    rec().end();
    await flush();
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.transcriptFinal).toBe(false);
    expect(c.snapshot.pending).not.toBeNull(); // the call stays pending; CloudGuard asks again
    expect(c.snapshot.message).toContain(PARTIAL_TEXT);

    const s2 = setup();
    await s2.ask();
    s2.promptEnds();
    s2.rec().open();
    s2.rec().end(); // ended with nothing heard: a fresh recognizer keeps listening until the window closes
    expect(s2.c.snapshot.status).toBe("listening");
    s2.timers.advance(LISTEN_TIMEOUT_MS);
    expect(s2.c.snapshot).toMatchObject({ status: "awaiting_confirmation", error: CONFIRM_TIMEOUT_TEXT });
    expect(s2.execute).not.toHaveBeenCalled();

    const s3 = setup();
    await s3.ask();
    s3.promptEnds();
    s3.rec().open();
    s3.rec().interim("yes");
    s3.rec().error("no-speech"); // Chrome reports no-speech after returning words
    await flush();
    expect(s3.execute).not.toHaveBeenCalled(); // still interim only: asked again, nothing run
  });

  it("9. retry after a recognition failure listens for the same call", async () => {
    const { c, rec, ask, promptEnds, flush, execute } = setup();
    await ask();
    promptEnds();
    const id = c.snapshot.pending!.id;
    rec().error("network");
    rec().end();
    expect(c.snapshot.status).toBe("error");
    c.startListening(); // Try again
    rec().open();
    expect(c.snapshot).toMatchObject({ status: "listening", listenMode: "confirmation" });
    expect(c.snapshot.pending!.id).toBe(id);
    rec().final("yes");
    rec().end();
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("silence: the timeout counts from onstart and nothing runs", async () => {
    const { c, rec, timers, ask, promptEnds, execute } = setup();
    await ask();
    promptEnds();
    rec().open();
    timers.advance(LISTEN_TIMEOUT_MS);
    expect(rec().aborted).toBe(1);
    expect(c.snapshot).toMatchObject({ status: "awaiting_confirmation", error: CONFIRM_TIMEOUT_TEXT });
    expect(execute).not.toHaveBeenCalled();
  });
});

describe("10-12: stale events, duplicates, disconnect", () => {
  it("10. a late YES after cancellation runs nothing", async () => {
    const { c, rec, ask, promptEnds, flush, execute } = setup();
    await ask();
    promptEnds();
    const session = rec();
    session.open();
    c.cancel();
    session.final("yes");
    session.end();
    await flush();
    expect(execute).not.toHaveBeenCalled();
  });

  it("11. repeated YES events execute once; no second session starts while one is open", async () => {
    let resolve!: (r: ExecuteResult) => void;
    const { c, recs, rec, timers, ask, promptEnds, flush, execute } = setup({ result: () => new Promise((r) => { resolve = r; }) });
    await ask();
    promptEnds();
    c.startListening(); // a manual tap while auto-listening is starting
    c.startListening();
    expect(recs).toHaveLength(2);
    const session = rec();
    session.open();
    session.final("yes");
    session.final("yes");
    session.end();
    session.end();
    session.final("yes yes");
    await flush();
    timers.advance(START_TIMEOUT_MS + LISTEN_TIMEOUT_MS);
    resolve({ speech: "Done." });
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("12. disconnect while listening aborts the session; nothing is scheduled or run afterwards", async () => {
    const { c, recs, rec, synth, timers, ask, promptEnds, flush, execute } = setup();
    await ask();
    promptEnds();
    const session = rec();
    session.open();
    session.interim("ye");
    c.reset();
    expect(session.aborted).toBe(1);
    expect(c.snapshot).toMatchObject({ status: "idle", pending: null, listenMode: null, interim: "" });
    session.final("yes");
    session.end();
    await flush();
    timers.advance(120_000);
    expect(recs).toHaveLength(2);
    expect(execute).not.toHaveBeenCalled();
    expect(synth.overlaps).toBe(0);
  });

  it("disconnect while starting (before onstart)", async () => {
    const { c, rec, timers, ask, promptEnds } = setup();
    await ask();
    promptEnds();
    c.reset();
    expect(rec().aborted).toBe(1);
    rec().open(); // a late onstart
    expect(c.snapshot.status).toBe("idle");
    timers.advance(START_TIMEOUT_MS + STOP_GRACE_MS);
    expect(c.snapshot.error).toBeNull();
  });
});

describe("recognition language", () => {
  it("uses the configured language and can be changed for the next session", () => {
    const { c, rec } = setup();
    c.setRecognitionLang("en-IN");
    c.startListening();
    expect(rec().lang).toBe("en-IN");
    c.setRecognitionLang("not a language");
    expect(c.snapshot.recognitionLang).toBe("en-IN");
  });

  it.each([
    [["en-IN", "en"], "UTC", "en-IN"], [["en-US"], "Asia/Kolkata", "en-IN"], [["en-US"], "Asia/Calcutta", "en-IN"],
    [["en-GB", "en"], "Europe/London", "en-GB"], [["en-us"], "America/New_York", "en-US"], [["fr-FR"], "Europe/Paris", "en-US"],
    [[], "", "en-US"],
  ])("defaultRecognitionLang(%j, %s) -> %s", (langs, tz, want) => {
    expect(defaultRecognitionLang(langs as string[], tz as string)).toBe(want);
  });
});
