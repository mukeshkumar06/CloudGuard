// Voice selection: the pure chooser and the controller's use of it, with a fake speechSynthesis. No browser.
import { describe, expect, it, vi } from "vitest";

import { PREVIEW_TEXT, VoiceController, type RecognitionLike, type SynthLike, type UtteranceLike, type VoiceDeps } from "../src/lib/voice";
import { chooseVoice, voiceGender, type VoiceLike, type VoicePreference } from "../src/lib/voices";

const v = (name: string, lang: string, extra: Partial<VoiceLike> = {}): VoiceLike => ({ name, lang, ...extra });

// Typical Windows + Edge/Chrome voices.
const ZIRA = v("Microsoft Zira - English (United States)", "en-US", { default: true });
const DAVID = v("Microsoft David - English (United States)", "en-US");
const HEERA = v("Microsoft Heera - English (India)", "en-IN");
const RAVI = v("Microsoft Ravi - English (India)", "en-IN");
const GOOGLE_UK_F = v("Google UK English Female", "en-GB", { localService: false });
const GOOGLE_UK_M = v("Google UK English Male", "en-GB", { localService: false });
const GOOGLE_US = v("Google US English", "en-US", { localService: false }); // no gender hint
const HINDI = v("Microsoft Kalpana - Hindi (India)", "hi-IN");
const GERMAN_M = v("Microsoft Stefan - German (Germany)", "de-DE");

describe("voiceGender", () => {
  it.each([[ZIRA, "female"], [DAVID, "male"], [HEERA, "female"], [RAVI, "male"], [GOOGLE_UK_F, "female"],
    [GOOGLE_UK_M, "male"], [GOOGLE_US, null], [v("Samantha", "en-US"), "female"], [v("Daniel", "en-GB"), "male"],
    [v("Microsoft Aria Online (Natural) - English (United States)", "en-US"), "female"], [v("Mystery", "en-US"), null]])(
    "%o -> %s", (voice, want) => expect(voiceGender(voice as VoiceLike)).toBe(want));
});

describe("chooseVoice", () => {
  const ALL = [ZIRA, DAVID, HEERA, RAVI, GOOGLE_UK_F, GOOGLE_UK_M, GOOGLE_US, HINDI];

  it("selects a female and a male voice", () => {
    expect(chooseVoice([ZIRA, DAVID], "female")).toEqual({ voice: ZIRA, match: "preferred" });
    expect(chooseVoice([ZIRA, DAVID], "male")).toEqual({ voice: DAVID, match: "preferred" });
  });

  it("prefers Indian English", () => {
    expect(chooseVoice(ALL, "female").voice).toBe(HEERA);
    expect(chooseVoice(ALL, "male").voice).toBe(RAVI);
    expect(chooseVoice([...ALL].reverse(), "female").voice).toBe(HEERA); // order-independent
  });

  it("then the default English voice, then on-device voices", () => {
    expect(chooseVoice([GOOGLE_UK_F, ZIRA], "female").voice).toBe(ZIRA);
    expect(chooseVoice([GOOGLE_UK_M, DAVID], "male").voice).toBe(DAVID);
  });

  it("prefers a natural (neural) voice, such as Edge's Online (Natural) voices", () => {
    const ARIA = { name: "Microsoft Aria Online (Natural) - English (United States)", lang: "en-US", localService: false };
    const NEERJA = { name: "Microsoft Neerja Online (Natural) - English (India)", lang: "en-IN", localService: false };
    expect(chooseVoice([ZIRA, ARIA], "female").voice).toBe(ARIA);
    expect(chooseVoice([ZIRA, ARIA, NEERJA], "female").voice).toBe(NEERJA);
  });

  it("never picks a non-English voice", () => {
    expect(chooseVoice([HINDI, GERMAN_M], "female")).toEqual({ voice: null, match: "browser-default" });
    expect(chooseVoice([GERMAN_M, ZIRA], "male")).toEqual({ voice: ZIRA, match: "english-fallback" });
  });

  it("falls back to another English voice when the preferred kind is missing", () => {
    expect(chooseVoice([ZIRA, GOOGLE_US], "male")).toEqual({ voice: ZIRA, match: "english-fallback" });
    expect(chooseVoice([GOOGLE_US], "female")).toEqual({ voice: GOOGLE_US, match: "english-fallback" });
  });

  it("falls back to the browser default with no voices, and for System default", () => {
    expect(chooseVoice([], "female")).toEqual({ voice: null, match: "browser-default" });
    expect(chooseVoice(ALL, "system")).toEqual({ voice: null, match: "browser-default" });
  });
});

// ---------- controller ----------

class FakeSynth implements SynthLike {
  voices: VoiceLike[] = [];
  spoken: UtteranceLike[] = [];
  active: UtteranceLike | null = null;
  overlaps = 0;
  private listeners = new Set<() => void>();
  getVoices() {
    return this.voices;
  }
  addEventListener(_: "voiceschanged", fn: () => void) {
    this.listeners.add(fn);
  }
  removeEventListener(_: "voiceschanged", fn: () => void) {
    this.listeners.delete(fn);
  }
  setVoices(vs: VoiceLike[]) {
    this.voices = vs;
    for (const fn of this.listeners) fn();
  }
  get listenerCount() {
    return this.listeners.size;
  }
  speak(u: UtteranceLike) {
    if (this.active) this.overlaps++;
    this.active = u;
    this.spoken.push(u);
  }
  cancel() {
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

function setup(voices: VoiceLike[] = [], pref?: VoicePreference) {
  const synth = new FakeSynth();
  synth.voices = voices;
  const holder: { rec: { onresult: ((e: unknown) => void) | null; onend: (() => void) | null } | null } = { rec: null };
  const execute = vi.fn<VoiceDeps["execute"]>(async () => ({ speech: "Server speech." }));
  const deps: VoiceDeps = {
    createRecognition: () => {
      const r: RecognitionLike = { lang: "", interimResults: false, continuous: false, maxAlternatives: 1,
                                   onresult: null, onerror: null, onend: null, onstart: null,
                                   start() { this.onstart?.(); }, stop() { this.onend?.(); },
                                   abort() {} };
      holder.rec = r as never;
      return r;
    },
    synth,
    createUtterance: (text) => ({ text, lang: "", onend: null, onerror: null }),
    tools: () => ["get_monthly_aws_bill"],
    execute,
  };
  const c = new VoiceController(deps, pref, { autoListen: false });
  const ask = async (text: string) => {
    c.startListening();
    holder.rec!.onresult!({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: true })] });
    holder.rec!.onend!();
    await new Promise((r) => setTimeout(r, 0));
  };
  return { c, synth, execute, ask };
}

describe("controller voice selection", () => {
  it("applies the chosen voice to confirmation prompts, server speech and replay", async () => {
    const { c, synth, ask } = setup([ZIRA, DAVID, HEERA, RAVI]);
    expect(c.snapshot).toMatchObject({ voicePreference: "female", voiceName: HEERA.name, voiceMatch: "preferred" });
    await ask("What was my AWS bill last month?");
    await ask("yes");
    synth.finish();
    c.replay();
    expect(synth.spoken.map((u) => u.text)).toEqual(
      ["I can check your AWS bill for last month. Shall I continue?", "Server speech.", "Server speech."]);
    for (const u of synth.spoken) expect(u).toMatchObject({ voice: HEERA, lang: "en-IN" });
  });

  it("male and system default", () => {
    const { c, synth } = setup([ZIRA, DAVID], "male");
    expect(c.snapshot.voiceName).toBe(DAVID.name);
    c.preview();
    expect(synth.spoken[0]).toMatchObject({ voice: DAVID, lang: "en-US" });
    c.setVoicePreference("system");
    c.preview();
    expect(synth.spoken[1].voice).toBeUndefined(); // the browser picks
    expect(synth.spoken[1].lang).toBe("en-US");
    expect(c.snapshot).toMatchObject({ voiceName: null, voiceMatch: "browser-default" });
  });

  it("missing preferred voice falls back to English, then the browser default", () => {
    const { c, synth } = setup([ZIRA, HINDI], "male");
    expect(c.snapshot).toMatchObject({ voiceName: ZIRA.name, voiceMatch: "english-fallback" });
    synth.setVoices([HINDI]);
    expect(c.snapshot).toMatchObject({ voiceName: null, voiceMatch: "browser-default", voicesAvailable: 1 });
    c.preview();
    expect(synth.spoken[0].voice).toBeUndefined();
  });

  it("no voices initially, then voiceschanged", () => {
    const { c, synth } = setup([]);
    expect(c.snapshot).toMatchObject({ voiceName: null, voiceMatch: "browser-default", voicesAvailable: 0 });
    c.preview(); // still speaks, with the browser default
    expect(synth.spoken[0].voice).toBeUndefined();
    synth.finish();
    synth.setVoices([ZIRA, HEERA]);
    expect(c.snapshot).toMatchObject({ voiceName: HEERA.name, voiceMatch: "preferred", voicesAvailable: 2 });
    c.preview();
    expect(synth.spoken[1].voice).toBe(HEERA);
  });

  it("getVoices throwing or missing is treated as no voices", () => {
    const synth = { speak() {}, cancel() {}, getVoices() { throw new Error("nope"); } };
    const c = new VoiceController({ createRecognition: null, synth, createUtterance: (text) => ({ text, lang: "", onend: null, onerror: null }),
                                    tools: () => [], execute: async () => ({}) });
    expect(c.snapshot.voicesAvailable).toBe(0);
    const bare = new VoiceController({ createRecognition: null, synth: { speak() {}, cancel() {} },
                                       createUtterance: (text) => ({ text, lang: "", onend: null, onerror: null }),
                                       tools: () => [], execute: async () => ({}) });
    expect(bare.snapshot.voiceMatch).toBe("browser-default");
  });

  it("preview speaks a sample without replacing the reply or replay text, and runs nothing", async () => {
    const { c, synth, execute, ask } = setup([ZIRA]);
    await ask("What was my AWS bill last month?");
    const prompt = c.snapshot.lastSpoken;
    c.preview();
    expect(synth.spoken.at(-1)).toMatchObject({ text: PREVIEW_TEXT, voice: ZIRA });
    expect(c.snapshot).toMatchObject({ status: "speaking", lastSpoken: prompt, message: prompt });
    synth.finish();
    expect(c.snapshot.status).toBe("awaiting_confirmation"); // the pending call is untouched
    expect(execute).not.toHaveBeenCalled();
    expect(synth.overlaps).toBe(0);
  });

  it("preview respects mute, listening and execution", () => {
    const { c, synth } = setup([ZIRA]);
    c.setMuted(true);
    c.preview();
    c.setMuted(false);
    c.startListening();
    c.preview();
    expect(synth.spoken).toHaveLength(0);
  });

  it("changing the voice while speaking does not cut off or overlap the current utterance", () => {
    const { c, synth } = setup([ZIRA, DAVID]);
    c.preview();
    const first = synth.active;
    c.setVoicePreference("male");
    synth.setVoices([ZIRA, DAVID, HEERA]); // the list can change mid-utterance too
    expect(synth.active).toBe(first);
    expect(c.snapshot.status).toBe("speaking");
    synth.finish();
    c.preview();
    expect(synth.spoken.map((u) => u.voice)).toEqual([ZIRA, DAVID]);
    expect(synth.overlaps).toBe(0);
  });

  it("disconnect keeps the preference, and dispose stops listening for voice changes", () => {
    const { c, synth } = setup([ZIRA, DAVID], "male");
    c.reset();
    expect(c.snapshot).toMatchObject({ voicePreference: "male", voiceName: DAVID.name });
    expect(synth.listenerCount).toBe(1);
    c.dispose();
    expect(synth.listenerCount).toBe(0);
  });
});
