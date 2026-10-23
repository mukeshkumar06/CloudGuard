// Long listening: about a minute per session, browser-initiated ends restarted, pauses tolerated, and no restart
// loops, duplicate transcripts or duplicate calls. Silence and partial words are never an answer, and confirmation is
// still required for any non-read-only call. Fake Web Speech APIs and fake timers; no browser, no microphone.
import { describe, expect, it, vi } from "vitest";

import {
  CONFIRM_TIMEOUT_TEXT, LISTEN_TIMEOUT_MS, MAX_RESTARTS, QUESTION_TIMEOUT_TEXT, SILENCE_MS, STOP_GRACE_MS,
  VoiceController, recognitionErrorMessage, type RecognitionLike, type Timers, type VoiceDeps,
} from "../src/lib/voice";

class Rec implements RecognitionLike {
  lang = ""; interimResults = false; continuous = false; maxAlternatives = 0;
  onresult: RecognitionLike["onresult"] = null;
  onerror: RecognitionLike["onerror"] = null;
  onend: RecognitionLike["onend"] = null;
  onstart: RecognitionLike["onstart"] = null;
  onspeechstart: RecognitionLike["onspeechstart"] = null;
  started = 0; stopped = 0; aborted = 0;
  results: { transcript: string; isFinal: boolean }[] = [];
  start() { this.started++; this.onstart?.(); }
  stop() { this.stopped++; }
  abort() { this.aborted++; this.onerror?.({ error: "aborted" }); this.onend?.(); }
  speech() { this.onspeechstart?.(); }
  /** Continuous mode: the results list grows; resultIndex is the first changed entry. */
  push(transcript: string, isFinal: boolean, resultIndex = this.results.length) {
    this.results[resultIndex] = { transcript, isFinal };
    this.onresult?.({ resultIndex, results: this.results.map((r) => Object.assign([{ transcript: r.transcript }], { isFinal: r.isFinal })) });
  }
  /** Chrome resending the whole list, earlier finals included. */
  resend() { this.onresult?.({ resultIndex: 0, results: this.results.map((r) => Object.assign([{ transcript: r.transcript }], { isFinal: r.isFinal })) }); }
  end() { this.onend?.(); }
  error(code: string) { this.onerror?.({ error: code }); }
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

function setup(needsConfirmation = false) {
  const recs: Rec[] = [];
  const timers = new FakeTimers();
  const execute = vi.fn<VoiceDeps["execute"]>(async () => ({ speech: "Done." }));
  const c = new VoiceController({
    createRecognition: () => { const r = new Rec(); recs.push(r); return r; },
    synth: null, createUtterance: null, tools: () => ["get_aws_account_identity", "get_monthly_aws_bill"],
    needsConfirmation: () => needsConfirmation, execute, timers,
  }, undefined, { autoListen: false });
  const rec = () => recs[recs.length - 1];
  const flush = () => new Promise((r) => setTimeout(r, 0));
  return { c, recs, rec, timers, execute, flush };
}

describe("a listening session lasts about a minute", () => {
  it("listens for the whole window before saying nothing was heard", () => {
    const { c, timers, execute } = setup();
    c.startListening();
    timers.advance(LISTEN_TIMEOUT_MS - 1);
    expect(c.snapshot.status).toBe("listening");
    timers.advance(1);
    expect(c.snapshot).toMatchObject({ status: "idle", error: QUESTION_TIMEOUT_TEXT });
    expect(execute).not.toHaveBeenCalled();
    expect(LISTEN_TIMEOUT_MS).toBeGreaterThanOrEqual(55_000);
  });

  it("a browser-initiated end with nothing heard restarts recognition, within the same window", () => {
    const { c, recs, rec, timers } = setup();
    c.startListening();
    timers.advance(7000);
    rec().error("no-speech"); // Chrome gives up after a few silent seconds...
    rec().end();
    expect(recs).toHaveLength(2); // ...and a fresh recognizer takes over
    expect(c.snapshot.status).toBe("listening");
    timers.advance(20_000);
    rec().end(); // ended again (network blip)
    expect(recs).toHaveLength(3);
    timers.advance(LISTEN_TIMEOUT_MS - 27_000); // the window still counts from the first onstart
    expect(c.snapshot).toMatchObject({ status: "idle", error: QUESTION_TIMEOUT_TEXT });
    expect(recs[2].aborted).toBe(1);
  });

  it("a question spoken after a restart is answered once", async () => {
    const { c, recs, rec, timers, execute, flush } = setup();
    c.startListening();
    rec().end();
    rec().push("Which AWS account am I using?", true);
    timers.advance(SILENCE_MS);
    expect(rec().stopped).toBe(1);
    rec().end();
    await flush();
    expect(recs).toHaveLength(2);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith("get_aws_account_identity", {});
  });

  it("restarts are capped, so a browser that keeps ending can't loop", () => {
    const { c, recs, rec } = setup();
    c.startListening();
    for (let i = 0; i < MAX_RESTARTS + 5; i++) rec().end();
    expect(recs).toHaveLength(MAX_RESTARTS + 1);
    expect(c.snapshot).toMatchObject({ status: "idle", error: QUESTION_TIMEOUT_TEXT });
  });
});

describe("pauses and silence", () => {
  it("a short pause doesn't end the question; SILENCE_MS after the last words does", async () => {
    const { c, rec, timers, execute, flush } = setup();
    c.startListening();
    rec().speech();
    rec().push("What was my AWS bill", true);
    timers.advance(SILENCE_MS - 500); // a pause to think
    expect(rec().stopped).toBe(0);
    rec().push("last month?", true);
    timers.advance(SILENCE_MS - 1);
    expect(rec().stopped).toBe(0);
    timers.advance(1);
    expect(rec().stopped).toBe(1); // graceful stop: final words can still arrive
    rec().end();
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith("get_monthly_aws_bill", { month: "last-month" });
  });

  it("a recognizer that never ends after stop() is wrapped up with what was heard", async () => {
    const { c, rec, timers, execute, flush } = setup();
    c.startListening();
    rec().push("Which AWS account am I using?", false); // only interim words
    timers.advance(SILENCE_MS + STOP_GRACE_MS);
    await flush();
    expect(rec().aborted).toBe(1);
    expect(execute).toHaveBeenCalledTimes(1);
  });
});

describe("no duplicates", () => {
  it("a final result resent by the browser is counted once", async () => {
    const { c, rec, timers, flush } = setup();
    c.startListening();
    rec().push("Which AWS", true);
    rec().push("account am I using?", true);
    rec().resend();
    rec().resend();
    timers.advance(SILENCE_MS);
    rec().end();
    await flush();
    expect(c.snapshot.transcript).toBe("Which AWS account am I using?");
  });

  it("late events after the answer never run a second call", async () => {
    const { c, recs, rec, timers, execute, flush } = setup();
    c.startListening();
    rec().push("Which AWS account am I using?", true);
    timers.advance(SILENCE_MS);
    const ended = rec();
    ended.end();
    await flush();
    ended.push("Which AWS account am I using?", true);
    ended.end();
    timers.advance(LISTEN_TIMEOUT_MS);
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
    expect(recs).toHaveLength(1); // and nothing restarted after the session finished
  });
});

describe("stop, cancel, disconnect", () => {
  it("manual stop uses what was heard and never restarts", async () => {
    const { c, recs, rec, timers, execute, flush } = setup();
    c.startListening();
    rec().push("Which AWS account am I using?", false);
    c.stopListening();
    expect(rec().stopped).toBe(1);
    rec().end();
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
    timers.advance(LISTEN_TIMEOUT_MS);
    expect(recs).toHaveLength(1);
  });

  it("manual stop with nothing heard is 'didn't hear', never an answer, and doesn't restart", () => {
    const { c, recs, rec, execute } = setup();
    c.startListening();
    c.stopListening();
    rec().end();
    expect(c.snapshot).toMatchObject({ status: "idle", error: QUESTION_TIMEOUT_TEXT });
    expect(recs).toHaveLength(1);
    expect(execute).not.toHaveBeenCalled();
  });

  it("cancel discards the words and nothing restarts", () => {
    const { c, recs, rec, timers, execute } = setup();
    c.startListening();
    rec().push("Which AWS account am I using?", true);
    c.cancelListening();
    expect(rec().aborted).toBe(1);
    timers.advance(LISTEN_TIMEOUT_MS);
    expect(recs).toHaveLength(1);
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.status).toBe("idle");
  });

  it("disconnect (reset) mid-session stops everything, with no restart", () => {
    const { c, recs, rec, timers, execute } = setup();
    c.startListening();
    rec().push("Which AWS", false);
    c.reset();
    rec().end();
    timers.advance(LISTEN_TIMEOUT_MS);
    expect(recs).toHaveLength(1);
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.status).toBe("idle");
  });
});

describe("permission and support", () => {
  it.each(["not-allowed", "service-not-allowed", "audio-capture"])("%s stops with a message and never restarts", (code) => {
    const { c, recs, rec, timers } = setup();
    c.startListening();
    rec().error(code);
    rec().end();
    timers.advance(LISTEN_TIMEOUT_MS);
    expect(recs).toHaveLength(1);
    expect(c.snapshot).toMatchObject({ status: "error", error: recognitionErrorMessage(code) });
  });

  it("an unsupported browser says so, and typing still works", () => {
    const c = new VoiceController({ createRecognition: null, synth: null, createUtterance: null, tools: () => [],
                                    execute: async () => ({}) }, undefined, { autoListen: false });
    c.startListening();
    expect(c.snapshot.status).toBe("error");
    expect(c.snapshot.error).toMatch(/isn't supported/);
  });
});

describe("confirmation is still required, and silence is never a yes", () => {
  it("a long session waiting for confirmation: silence, partial words and 'sounds good' never confirm", async () => {
    const { c, recs, rec, timers, execute, flush } = setup(true);
    await c.handleTranscript("Which AWS account am I using?");
    expect(c.snapshot.status).toBe("awaiting_confirmation");
    c.startListening();
    rec().end(); // browser end, nothing heard: restarted
    timers.advance(30_000);
    rec().push("ye", false); // a partial word...
    c.cancelListening(); // ...then cancelled: never confirms
    expect(execute).not.toHaveBeenCalled();
    c.startListening();
    rec().push("sounds good", true);
    timers.advance(SILENCE_MS);
    rec().end();
    await flush();
    expect(execute).not.toHaveBeenCalled(); // ambiguous: asked again
    expect(c.snapshot.pending).not.toBeNull();
    c.startListening();
    timers.advance(LISTEN_TIMEOUT_MS); // a whole minute of silence
    expect(c.snapshot).toMatchObject({ status: "awaiting_confirmation", error: CONFIRM_TIMEOUT_TEXT });
    expect(execute).not.toHaveBeenCalled();
    expect(recs.length).toBeGreaterThan(2);
  });

  it("refusal still wins after a pause", async () => {
    const { c, rec, timers, execute, flush } = setup(true);
    await c.handleTranscript("Which AWS account am I using?");
    c.startListening();
    rec().push("yes", true);
    timers.advance(SILENCE_MS - 1000);
    rec().push("no, don't", true);
    timers.advance(SILENCE_MS);
    rec().end();
    await flush();
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.pending).toBeNull();
  });
});
