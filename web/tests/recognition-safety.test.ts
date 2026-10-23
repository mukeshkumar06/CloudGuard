// Speech recognition safety and playback consistency (voice.ts): misheard or unfinished speech never starts a
// tagging search, a spoken tagging request is read back as heard before anything runs, a typed correction can only
// replace the request, and speech output keeps one voice and one volume and never overlaps the microphone.
// Fake recognizers, fake speech engine, fake tools: no browser, no network, no AWS.
import { describe, expect, it, vi } from "vitest";

import type { ConfirmOut, OperatorApi, PrepareOut } from "../src/lib/operator";
import { READ_ONLY_TOOLS } from "../src/lib/tools";
import { mapIntent } from "../src/lib/intent";
import { LOW_CONFIDENCE, PARTIAL_TEXT, REQUEST_AGAIN_TEXT, SPEECH_SETTINGS, VoiceController, type RecognitionLike,
         type SynthLike, type Timers, type UtteranceLike, type VoiceDeps } from "../src/lib/voice";

const TOOLS = [...READ_ONLY_TOOLS, "preview_tag_changes"];
const SEARCH = { mode: "fixture", total: 1, resources: [{ display_name: "codebridge-api" }] };
const READY: PrepareOut = { status: "READY", mode: "fixture", resources: [], overwrites: [], requires_overwrite_ack: false,
                            challenge: { id: "ch-1", expires_at: "2026-10-23T09:02:00Z", plan_hash: "sha256:x" } };
const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0)); };

class FakeTimers implements Timers {
  now = 0;
  private q: { at: number; fn: () => void; id: number }[] = [];
  private n = 0;
  set(fn: () => void, ms: number) { const id = ++this.n; this.q.push({ at: this.now + ms, fn, id }); return id; }
  clear(h: unknown) { this.q = this.q.filter((t) => t.id !== h); }
  advance(ms: number) {
    const end = this.now + ms;
    for (;;) {
      this.q.sort((a, b) => a.at - b.at);
      const t = this.q[0];
      if (!t || t.at > end) break;
      this.q.shift();
      this.now = t.at;
      t.fn();
    }
    this.now = end;
  }
}

class Rec implements RecognitionLike {
  lang = ""; interimResults = false; continuous = false; maxAlternatives = 0;
  onresult: RecognitionLike["onresult"] = null; onerror: RecognitionLike["onerror"] = null; onend: RecognitionLike["onend"] = null;
  onstart: (() => void) | null = null; onspeechstart: (() => void) | null = null;
  results: ({ transcript: string; confidence?: number }[] & { isFinal: boolean })[] = [];
  start() { this.onstart?.(); }
  stop() { this.onend?.(); }
  abort() {}
  say(text: string, isFinal: boolean, confidence?: number) {
    const r = Object.assign([{ transcript: text, confidence }], { isFinal });
    if (this.results.length && !this.results[this.results.length - 1].isFinal) this.results[this.results.length - 1] = r;
    else this.results.push(r);
    this.onresult?.({ resultIndex: this.results.length - 1, results: this.results });
  }
}

function setup(opts: { operator?: boolean; synth?: SynthLike | null } = {}) {
  const api = { prepare: vi.fn<OperatorApi["prepare"]>(async () => READY),
                confirm: vi.fn<OperatorApi["confirm"]>(async () => ({ status: "EXECUTED" } as ConfirmOut)),
                cancel: vi.fn<OperatorApi["cancel"]>(async () => {}) };
  const call = vi.fn<NonNullable<VoiceDeps["call"]>>(async (tool) => ({
    structured: tool === "search_resources" ? SEARCH : { status: "PROPOSAL_READY", request: {} } }));
  const recs: Rec[] = [];
  const timers = new FakeTimers();
  const c = new VoiceController({
    createRecognition: () => { const r = new Rec(); recs.push(r); return r; },
    synth: opts.synth ?? null, createUtterance: opts.synth ? (text) => ({ text, onend: null, onerror: null } as UtteranceLike) : null,
    tools: () => TOOLS, needsConfirmation: () => true, execute: vi.fn(), call, timers,
    selectedRegion: () => "ap-south-1", operator: () => (opts.operator === false ? null : api),
  }, undefined, { autoListen: false, recognitionLang: "en-IN" });
  return { c, api, call, recs, timers };
}

/** Speak one request through a real listening session: optional interim words, then the final words (if any). */
async function speak(s: ReturnType<typeof setup>, words: string, opts: { final?: boolean; confidence?: number } = {}) {
  s.c.startListening();
  const r = s.recs[s.recs.length - 1];
  r.say(words, false);
  if (opts.final !== false) r.say(words, true, opts.confidence ?? 0.92);
  s.c.stopListening();
  s.timers.advance(5000);
  await tick();
}

describe("what the test phrases mean (deterministic rules only)", () => {
  it.each([
    ["Find all the Lambdas which has CodeBridge and tag them.", "tag"],
    ["Find all Lambda functions containing CodeBridge and tag them.", "tag"],
    ["Find untagged Lambda functions.", "needs"], // which region? first
    ["How many AWS resources do I have in Mumbai?", "call"],
    ["Show me DynamoDB tables.", "needs"],
    ["Show me all DynamoDB tables in Singapore.", "call"],
    ["Find resources in Mumbai.", "call"],
    ["Find all Lambda functions containing CodeBridge in ap-south-1 and tag them with owner=mukesh.", "tag"],
  ])("%s -> %s", (q, kind) => {
    expect(mapIntent(q, TOOLS).kind).toBe(kind);
  });

  it("the misheard request names no supported service, so nothing is searched or tagged", async () => {
    const s = setup();
    await speak(s, "Find all the alarms with name Godrej unit and tag them.");
    expect(s.call).not.toHaveBeenCalled();
    expect(s.api.prepare).not.toHaveBeenCalled();
    expect(s.c.snapshot.heardCheck).toBeNull();
  });
});

describe("recognition settings", () => {
  it("uses the selected language for each new session, interim and final results kept apart", async () => {
    const s = setup();
    s.c.startListening();
    expect(s.recs[0]).toMatchObject({ lang: "en-IN", interimResults: true, continuous: true });
    s.recs[0].say("Find all", false);
    expect(s.c.snapshot.interim).toBe("Find all");
    expect(s.c.snapshot.transcript).toBe(""); // interim words are not a transcript
    s.c.cancelListening();
    s.c.setRecognitionLang("en-US");
    s.c.startListening();
    expect(s.recs[1].lang).toBe("en-US");
  });
});

describe("tagging requests are read back as heard before anything runs", () => {
  it("shows and says the exact transcript and its meaning, and searches only after a spoken yes", async () => {
    const s = setup();
    await speak(s, "Find all the Lambdas which has CodeBridge and tag them.");
    expect(s.c.snapshot.transcript).toBe("Find all the Lambdas which has CodeBridge and tag them.");
    expect(s.c.snapshot.heardCheck?.prompt).toContain('I heard: "Find all the Lambdas which has CodeBridge and tag them."');
    expect(s.c.snapshot.message).toContain("That means Lambda functions with names containing CodeBridge.");
    expect(s.c.snapshot.message).not.toContain("Mumbai"); // no region was said, so none is assumed
    expect(s.call).not.toHaveBeenCalled();
    await speak(s, "yes");
    expect(s.call).not.toHaveBeenCalled(); // the region comes next, never the selected default
    expect(s.c.snapshot.message).toMatch(/^Which AWS region should I search for the Lambda functions/);
    expect(s.c.snapshot.message).not.toContain("all regions"); // tagging is one region at a time
    await speak(s, "Singapore");
    expect(s.call).toHaveBeenCalledWith("search_resources", expect.objectContaining({ services: ["lambda"], region: "ap-southeast-1" }));
    expect(s.api.prepare).not.toHaveBeenCalled(); // the tag key and value are still to come, then the server's plan
  });

  it("no, or an unclear answer, runs nothing", async () => {
    const s = setup();
    await speak(s, "Find all Lambda functions containing CodeBridge and tag them.");
    await speak(s, "maybe");
    expect(s.c.snapshot.heardCheck).not.toBeNull();
    await speak(s, "no");
    expect(s.c.snapshot.heardCheck).toBeNull();
    expect(s.c.snapshot.message).toBe(REQUEST_AGAIN_TEXT);
    expect(s.call).not.toHaveBeenCalled();
  });

  it("words the recognizer never finalised don't start or confirm a tagging request", async () => {
    const s = setup();
    await speak(s, "Find all Lambda functions containing CodeBridge and tag them.", { final: false });
    expect(s.c.snapshot.transcriptFinal).toBe(false);
    expect(s.c.snapshot.heardCheck).toBeNull();
    expect(s.c.snapshot.message).toContain(PARTIAL_TEXT);
    await speak(s, "Find all Lambda functions containing CodeBridge and tag them.");
    await speak(s, "yes", { final: false });
    expect(s.call).not.toHaveBeenCalled();
    expect(s.c.snapshot.heardCheck).not.toBeNull();
  });

  it("a low-confidence transcript is flagged in the read-back", async () => {
    const s = setup();
    await speak(s, "Find all Lambda functions containing CodeBridge and tag them.", { confidence: LOW_CONFIDENCE - 0.2 });
    expect(s.c.snapshot.transcriptConfidence).toBeCloseTo(LOW_CONFIDENCE - 0.2);
    expect(s.c.snapshot.message).toContain("I'm not sure I heard that correctly.");
  });

  it("the same read-back applies without an operator session (preview-only flow)", async () => {
    const s = setup({ operator: false });
    await speak(s, "Find all Lambda functions containing CodeBridge and tag them.");
    expect(s.c.snapshot.heardCheck).not.toBeNull();
    expect(s.call).not.toHaveBeenCalled();
  });
});

describe("typed correction of a misheard request", () => {
  it("replaces the request, but the search still waits for a spoken yes", async () => {
    const s = setup();
    await speak(s, "Find all the Lambdas which has Godrej unit and tag them.");
    expect(s.c.correctTranscript("Find all Lambda functions containing CodeBridge and tag them.")).toBe(true);
    expect(s.c.snapshot.heardCheck?.intent.filter.text).toBe("CodeBridge");
    expect(s.call).not.toHaveBeenCalled();
    await speak(s, "yes");
    expect(s.c.correctTranscript("Mumbai")).toBe(false); // typed text can't answer the region question either
    await speak(s, "Mumbai");
    expect(s.call).toHaveBeenCalledWith("search_resources", expect.objectContaining({ text: "CodeBridge", region: "ap-south-1" }));
  });

  it("can't answer a confirmation: typed text never confirms a write", async () => {
    const s = setup();
    await speak(s, "Find all Lambda functions containing CodeBridge and tag them.");
    await speak(s, "yes");
    await speak(s, "Mumbai");
    await speak(s, "Key is owner, value is mukesh.");
    expect(s.c.snapshot.writeFlow?.stage).toBe("confirm");
    expect(s.c.correctTranscript("yes")).toBe(false);
    await tick();
    expect(s.api.confirm).not.toHaveBeenCalled();
  });
});

describe("speech output", () => {
  class Synth implements SynthLike {
    speaking = false; pending = false;
    spoken: UtteranceLike[] = [];
    cancels = 0;
    speak(u: UtteranceLike) { this.spoken.push(u); this.speaking = true; }
    cancel() { this.cancels++; this.speaking = false; }
    getVoices() { return [{ name: "Microsoft Zira - English (United States)", lang: "en-US", default: true, localService: true }]; }
    finish() { const u = this.spoken[this.spoken.length - 1]; this.speaking = false; u.onend?.(); }
  }

  it("every utterance and its replay use the same voice and the same fixed volume, rate and pitch", async () => {
    const synth = new Synth();
    const s = setup({ synth });
    await speak(s, "How many AWS resources do I have in Mumbai?");
    synth.finish();
    s.c.replay();
    expect(synth.spoken.length).toBeGreaterThanOrEqual(2);
    for (const u of synth.spoken) {
      expect(u).toMatchObject({ volume: SPEECH_SETTINGS.volume, rate: SPEECH_SETTINGS.rate, pitch: SPEECH_SETTINGS.pitch });
      expect(u.voice).toBe(synth.spoken[0].voice);
    }
  });

  it("listening cancels an utterance that hasn't ended even if the engine claims to be idle", async () => {
    const synth = new Synth();
    const s = setup({ synth });
    await speak(s, "How many AWS resources do I have in Mumbai?");
    synth.speaking = false; // an engine that misreports mid-utterance
    const before = synth.cancels;
    s.c.startListening();
    expect(synth.cancels).toBe(before + 1);
  });
});
