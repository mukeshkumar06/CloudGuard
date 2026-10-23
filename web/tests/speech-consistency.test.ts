// Regression: every utterance (prompt, server speech, replay, preview) gets the same explicit volume, rate, pitch
// and selected voice, and cancellation leaves no stale utterance. Fake speechSynthesis; no browser.
import { describe, expect, it, vi } from "vitest";

import { SPEECH_SETTINGS, VoiceController, type RecognitionLike, type SynthLike, type UtteranceLike, type VoiceDeps } from "../src/lib/voice";
import type { VoiceLike } from "../src/lib/voices";

const HEERA: VoiceLike = { name: "Microsoft Heera - English (India)", lang: "en-IN" };
const ZIRA: VoiceLike = { name: "Microsoft Zira - English (United States)", lang: "en-US", default: true };

class Synth implements SynthLike {
  voices: VoiceLike[] = [];
  spoken: UtteranceLike[] = [];
  active: UtteranceLike | null = null;
  cancels = 0;
  overlaps = 0;
  get speaking() { return this.active !== null; }
  get pending() { return false; }
  getVoices() { return this.voices; }
  speak(u: UtteranceLike) {
    if (this.active) this.overlaps++;
    this.active = u;
    this.spoken.push(u);
  }
  cancel() {
    this.cancels++;
    const u = this.active;
    this.active = null;
    u?.onerror?.();
  }
  finish() {
    const u = this.active;
    this.active = null;
    u?.onend?.();
  }
}

function setup(voices: VoiceLike[]) {
  const synth = new Synth();
  synth.voices = voices;
  const holder: { rec: RecognitionLike | null } = { rec: null };
  const execute = vi.fn<VoiceDeps["execute"]>(async () => ({ speech: "Your bill was 12 dollars." }));
  const c = new VoiceController({
    createRecognition: () => {
      const r: RecognitionLike = { lang: "", interimResults: false, continuous: false, maxAlternatives: 1,
                                   onresult: null, onerror: null, onend: null, onstart: null,
                                   start() { this.onstart?.(); }, stop() {}, abort() {} };
      holder.rec = r;
      return r;
    },
    synth,
    // A fresh object each time, starting from odd values, so nothing passes by default.
    createUtterance: (text) => ({ text, lang: "", onend: null, onerror: null, volume: 0.3, rate: 1.7, pitch: 0.2 }),
    tools: () => ["get_monthly_aws_bill"],
    execute,
  }, undefined, { autoListen: false });
  const ask = async (text: string) => {
    c.startListening();
    holder.rec!.onresult!({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: true })] });
    holder.rec!.onend!();
    await new Promise((r) => setTimeout(r, 0));
  };
  return { c, synth, ask, execute };
}

const settings = (u: UtteranceLike) => ({ volume: u.volume, rate: u.rate, pitch: u.pitch, voice: u.voice, lang: u.lang });

describe("consistent speech configuration", () => {
  it("is explicit and identical for prompt, server speech, replay and preview", async () => {
    const { c, synth, ask } = setup([ZIRA, HEERA]);
    await ask("What was my AWS bill last month?");
    synth.finish();
    await ask("yes");
    synth.finish();
    c.replay();
    synth.finish();
    c.preview();
    synth.finish();
    c.replay();
    expect(synth.spoken).toHaveLength(5);
    for (const u of synth.spoken) {
      expect(settings(u)).toEqual({ ...SPEECH_SETTINGS, voice: HEERA, lang: "en-IN" });
    }
    expect(SPEECH_SETTINGS).toEqual({ volume: 1, rate: 1, pitch: 1 });
  });

  it("the first utterance uses the selected voice even if voiceschanged has not fired yet", () => {
    const { c, synth } = setup([]);
    synth.voices = [ZIRA, HEERA]; // the browser has them now, but announced nothing
    c.preview();
    synth.finish();
    c.preview();
    expect(synth.spoken.map((u) => u.voice)).toEqual([HEERA, HEERA]);
  });

  it("with no voices at all, every utterance still gets the same explicit settings", () => {
    const { c, synth } = setup([]);
    c.preview();
    synth.finish();
    c.preview();
    for (const u of synth.spoken) expect(settings(u)).toEqual({ ...SPEECH_SETTINGS, voice: undefined, lang: "en-US" });
  });

  it("interrupting speech never overlaps and leaves no stale utterance", async () => {
    const { c, synth, ask } = setup([HEERA]);
    await ask("What was my AWS bill last month?");
    const prompt = synth.active!;
    c.replay(); // interrupts the prompt
    c.preview(); // interrupts the replay
    expect(synth.overlaps).toBe(0);
    expect(synth.active).toBe(synth.spoken.at(-1));
    prompt.onend?.(); // a late event from a cancelled utterance changes nothing
    expect(c.snapshot.status).toBe("speaking");
    c.stopSpeaking();
    expect(synth.active).toBeNull();
    expect(c.snapshot.status).toBe("awaiting_confirmation");
    for (const u of synth.spoken) expect(settings(u)).toEqual({ ...SPEECH_SETTINGS, voice: HEERA, lang: "en-IN" });
  });

  it("an idle engine is not cancelled between consecutive utterances", () => {
    const { c, synth } = setup([HEERA]);
    c.preview();
    synth.finish();
    c.preview();
    synth.finish();
    expect(synth.cancels).toBe(0);
    c.preview();
    c.replay(); // nothing to replay yet: ignored
    c.preview(); // busy engine: cancelled first
    expect(synth.cancels).toBe(1);
  });
});
