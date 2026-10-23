// VoiceController with fake Web Speech APIs. No browser, no network: `execute` is a spy standing in for the app's
// MCP pipeline (the real transport is exercised by the Playwright suite).
import { describe, expect, it, vi } from "vitest";

import {
  QUESTION_TIMEOUT_TEXT, REPEAT_TEXT, confirmationOf, recognitionErrorMessage, VoiceController, type ExecuteResult, type RecognitionLike, type SynthLike,
  type UtteranceLike, type VoiceDeps,
} from "../src/lib/voice";

const TOOLS = ["get_monthly_aws_bill", "get_current_month_spending", "get_cost_breakdown", "compare_monthly_costs",
  "investigate_cost_increase", "get_cost_anomalies", "get_resource_inventory", "scan_tag_compliance",
  "list_untagged_resources", "explain_finding", "recommend_tags"];
const SERVER_SPEECH = "Your AWS bill for the September 2026 billing month was 12 dollars and 34 cents.";

class FakeRecognition implements RecognitionLike {
  lang = "";
  interimResults = false;
  continuous = true;
  maxAlternatives = 0;
  onresult: RecognitionLike["onresult"] = null;
  onerror: RecognitionLike["onerror"] = null;
  onend: RecognitionLike["onend"] = null;
  onstart: RecognitionLike["onstart"] = null;
  started = 0;
  stopped = 0;
  aborted = 0;
  startThrows = false;
  start() {
    if (this.startThrows) throw new Error("busy");
    this.started++;
    this.onstart?.(); // the browser opened the microphone
  }
  stop() {
    this.stopped++;
    this.onend?.();
  }
  abort() {
    // Browsers fire "aborted" and then "end" after abort(); the controller must ignore both.
    this.aborted++;
    this.onerror?.({ error: "aborted" });
    this.onend?.();
  }
  interim(text: string) {
    this.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: false })] });
  }
  final(text: string) {
    this.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: true })] });
  }
  /** A complete utterance: interim words, the final result, then the end of the session. */
  say(text: string) {
    this.interim(text.slice(0, Math.ceil(text.length / 2)));
    this.final(text);
    this.onend?.();
  }
  fail(code: string) {
    this.onerror?.({ error: code });
    this.onend?.();
  }
}

class FakeSynth implements SynthLike {
  spoken: UtteranceLike[] = [];
  active: UtteranceLike | null = null;
  cancels = 0;
  overlaps = 0;
  speak(u: UtteranceLike) {
    if (this.active) this.overlaps++;
    this.active = u;
    this.spoken.push(u);
  }
  cancel() {
    this.cancels++;
    const u = this.active;
    this.active = null;
    u?.onerror?.(); // Chrome reports "interrupted" for a cancelled utterance
  }
  finish() {
    const u = this.active;
    this.active = null;
    u?.onend?.();
  }
  get texts() {
    return this.spoken.map((u) => u.text);
  }
}

function setup(opts: { recognition?: boolean; synth?: boolean; result?: ExecuteResult | (() => Promise<ExecuteResult>) } = {}) {
  const recs: FakeRecognition[] = [];
  const synth = new FakeSynth();
  const result = opts.result ?? { speech: SERVER_SPEECH };
  const execute = vi.fn<VoiceDeps["execute"]>(typeof result === "function" ? result : async () => result);
  const deps: VoiceDeps = {
    createRecognition: opts.recognition === false ? null : () => {
      const r = new FakeRecognition();
      recs.push(r);
      return r;
    },
    synth: opts.synth === false ? null : synth,
    createUtterance: opts.synth === false ? null : (text) => ({ text, lang: "", onend: null, onerror: null }),
    tools: () => TOOLS,
    execute,
  };
  const c = new VoiceController(deps, undefined, { autoListen: false });
  const rec = () => recs[recs.length - 1];
  const flush = () => new Promise((r) => setTimeout(r, 0));
  /** Start listening and speak one utterance. */
  const ask = async (text: string) => {
    c.startListening();
    rec().say(text);
    await flush();
  };
  return { c, recs, rec, synth, execute, ask, flush };
}

describe("microphone", () => {
  it("starts and stops recognition", () => {
    const { c, rec } = setup();
    expect(c.snapshot.status).toBe("idle");
    c.startListening();
    expect(c.snapshot.status).toBe("listening");
    expect(rec().started).toBe(1);
    expect(rec().interimResults).toBe(true);
    expect(rec().continuous).toBe(true); // pauses don't end an answer; a silence timer does
    c.stopListening();
    expect(rec().stopped).toBe(1);
  });

  it("does not start a second session while listening", () => {
    const { c, recs } = setup();
    c.startListening();
    c.startListening();
    expect(recs).toHaveLength(1);
  });

  it("cancel discards what was heard and ignores the aborted events", async () => {
    const { c, rec, execute, synth, flush } = setup();
    c.startListening();
    rec().interim("what was my bill");
    expect(c.snapshot.interim).toBe("what was my bill");
    c.cancelListening();
    await flush();
    expect(rec().aborted).toBe(1);
    expect(c.snapshot).toMatchObject({ status: "idle", interim: "", error: null, pending: null });
    expect(execute).not.toHaveBeenCalled();
    expect(synth.spoken).toHaveLength(0);
  });

  it("reports a microphone that cannot start", () => {
    const { c } = setup();
    const orig = (c as unknown as { deps: VoiceDeps }).deps.createRecognition!;
    (c as unknown as { deps: VoiceDeps }).deps.createRecognition = () => Object.assign(orig(), { startThrows: true });
    c.startListening();
    expect(c.snapshot.status).toBe("error");
    expect(c.snapshot.error).toMatch(/busy|could not be started/);
  });
});

describe("recognition", () => {
  it("shows interim words, then the final transcript, and asks for confirmation", async () => {
    const { c, rec, execute, synth, flush } = setup();
    c.startListening();
    rec().interim("how much did AWS");
    expect(c.snapshot).toMatchObject({ status: "listening", interim: "how much did AWS" });
    rec().final("How much did AWS cost last month?");
    expect(c.snapshot.interim).toBe("");
    rec().onend?.();
    await flush();
    expect(c.snapshot.transcript).toBe("How much did AWS cost last month?");
    expect(c.snapshot.status).toBe("speaking");
    expect(c.snapshot.pending).toMatchObject({ tool: "get_monthly_aws_bill", args: { month: "last-month" } });
    expect(synth.texts).toEqual(["I can check your AWS bill for last month. Shall I continue?"]);
    synth.finish();
    expect(c.snapshot.status).toBe("awaiting_confirmation");
    expect(execute).not.toHaveBeenCalled(); // recognition alone never executes
  });

  it("permission denied", async () => {
    const { c, rec, execute } = setup();
    c.startListening();
    rec().fail("not-allowed");
    expect(c.snapshot.status).toBe("error");
    expect(c.snapshot.error).toMatch(/Microphone access was denied/);
    expect(execute).not.toHaveBeenCalled();
  });

  it("unsupported browser", () => {
    const { c } = setup({ recognition: false });
    expect(c.snapshot.recognitionSupported).toBe(false);
    c.startListening();
    expect(c.snapshot.status).toBe("error");
    expect(c.snapshot.error).toMatch(/isn't supported/);
  });

  it("empty transcript", async () => {
    const { c, rec, execute, synth, flush } = setup();
    c.startListening();
    rec().final("   ");
    rec().onend?.(); // the browser ended with nothing usable: listening goes on with a fresh recognizer
    await flush();
    expect(c.snapshot.status).toBe("listening");
    c.stopListening(); // the user stops: nothing was heard, so nothing is asked
    await flush();
    expect(c.snapshot.status).toBe("idle");
    expect(c.snapshot.error).toBe(QUESTION_TIMEOUT_TEXT);
    expect(execute).not.toHaveBeenCalled();
    expect(synth.spoken).toHaveLength(0);
  });

  it("silence (no-speech) keeps listening; stopping then reports not heard", () => {
    const { c, rec, execute } = setup();
    c.startListening();
    rec().fail("no-speech");
    expect(c.snapshot.status).toBe("listening"); // restarted, not given up
    c.stopListening();
    expect(c.snapshot).toMatchObject({ status: "idle", error: QUESTION_TIMEOUT_TEXT });
    expect(execute).not.toHaveBeenCalled();
  });

  it.each(["audio-capture", "network", "something-new"])("recognition error %s", (code) => {
    const { c, rec, execute } = setup();
    c.startListening();
    rec().fail(code);
    expect(c.snapshot).toMatchObject({ status: "error", error: recognitionErrorMessage(code) });
    expect(execute).not.toHaveBeenCalled();
  });

  it("a recognition error keeps the pending call pending", async () => {
    const { c, rec, ask, execute } = setup();
    await ask("What was my AWS bill last month?");
    const pending = c.snapshot.pending;
    c.startListening();
    rec().fail("no-speech");
    expect(c.snapshot.pending).toBe(pending);
    expect(execute).not.toHaveBeenCalled();
  });

  it("an unmatched question is answered without any call", async () => {
    const { c, ask, execute, synth } = setup();
    await ask("Tell me a joke");
    expect(c.snapshot.pending).toBeNull();
    expect(synth.texts[0]).toMatch(/No tool matches/);
    expect(execute).not.toHaveBeenCalled();
  });

  it("a write request is refused without any call", async () => {
    const { c, ask, execute, synth } = setup();
    await ask("Delete my untagged buckets");
    expect(c.snapshot.pending).toBeNull();
    expect(synth.texts[0]).toMatch(/read-only/);
    expect(execute).not.toHaveBeenCalled();
  });
});

describe("confirmation", () => {
  it.each(["yes", "Yes.", "confirm", "go ahead", "Go ahead, please"])("verbal %s executes exactly one MCP call", async (word) => {
    const { c, ask, execute, synth } = setup();
    await ask("How much did AWS cost last month?");
    await ask(word);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith("get_monthly_aws_bill", { month: "last-month" });
    expect(c.snapshot.pending).toBeNull();
    expect(c.snapshot.lastCall).toEqual({ tool: "get_monthly_aws_bill", outcome: "ok" });
    expect(synth.texts.at(-1)).toBe(SERVER_SPEECH);
    // Saying yes again has nothing left to confirm.
    synth.finish();
    await ask("yes");
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it.each(["no", "cancel", "No thanks"])("verbal %s executes zero MCP calls", async (word) => {
    const { c, ask, execute, synth } = setup();
    await ask("How much did AWS cost last month?");
    await ask(word);
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.pending).toBeNull();
    expect(synth.texts.at(-1)).toBe("Okay, cancelled. Nothing was run.");
  });

  it.each(["yes but check this month instead", "what about compliance", "I guess", "maybe"])(
    "unrelated speech %j does not confirm", async (text) => {
      const { c, ask, execute, synth } = setup();
      await ask("How much did AWS cost last month?");
      const pending = c.snapshot.pending;
      await ask(text);
      expect(execute).not.toHaveBeenCalled();
      expect(c.snapshot.pending).toBe(pending);
      expect(synth.texts.at(-1)).toBe(`${REPEAT_TEXT} ${pending!.prompt}`);
    });

  it("the confirm button runs the pending call once, and only with its id", async () => {
    const { c, ask, execute } = setup();
    await ask("Check my tagging compliance.");
    const id = c.snapshot.pending!.id;
    await c.confirm(id + 1); // a stale id
    expect(execute).not.toHaveBeenCalled();
    const first = c.confirm(id);
    const second = c.confirm(id); // double click
    await Promise.all([first, second]);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith("scan_tag_compliance", {});
  });

  it("a new question replaces nothing while a call is pending", async () => {
    const { c, ask } = setup();
    await ask("Check my tagging compliance.");
    const pending = c.snapshot.pending!;
    await ask("What was my AWS bill last month?");
    expect(c.snapshot.pending).toBe(pending);
  });

  it("pressing confirm while listening aborts recognition, so the late words do nothing", async () => {
    const { c, rec, ask, execute, flush } = setup();
    await ask("Check my tagging compliance.");
    c.startListening();
    const listening = rec();
    await c.confirm(c.snapshot.pending!.id);
    expect(listening.aborted).toBe(1);
    listening.final("yes"); // late events from the aborted session
    listening.onend?.();
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("a confirmation word with nothing pending runs nothing", async () => {
    const { ask, execute } = setup();
    await ask("yes");
    expect(execute).not.toHaveBeenCalled();
  });

  it("speaks a sanitized error when the call fails", async () => {
    const { c, ask, synth } = setup({ result: { errorText: "The server could not be reached." } });
    await ask("Are there any cost anomalies?");
    await ask("yes");
    expect(c.snapshot.lastCall).toEqual({ tool: "get_cost_anomalies", outcome: "error" });
    expect(c.snapshot.error).toBe("The server could not be reached.");
    expect(synth.texts.at(-1)).toBe("Sorry, that didn't work. The server could not be reached.");
  });

  it("a result that arrives after a reset is not spoken", async () => {
    let resolve!: (r: ExecuteResult) => void;
    const { c, ask, synth, flush } = setup({ result: () => new Promise((r) => { resolve = r; }) });
    await ask("Are there any cost anomalies?");
    const confirming = ask("yes");
    await flush();
    expect(c.snapshot.status).toBe("executing");
    c.reset();
    resolve({ speech: SERVER_SPEECH });
    await confirming;
    expect(synth.texts).not.toContain(SERVER_SPEECH);
    expect(c.snapshot.status).toBe("idle");
  });

  it("the microphone is unavailable while a call is executing", async () => {
    let resolve!: (r: ExecuteResult) => void;
    const { c, recs, ask, flush } = setup({ result: () => new Promise((r) => { resolve = r; }) });
    await ask("Are there any cost anomalies?");
    const confirming = ask("yes");
    await flush();
    const before = recs.length;
    c.startListening();
    expect(recs).toHaveLength(before);
    resolve({ speech: SERVER_SPEECH });
    await confirming;
  });
});

describe("spoken output", () => {
  it("speaks the server's speech field verbatim, never structured data", async () => {
    const { ask, synth } = setup();
    await ask("What was my AWS bill last month?");
    await ask("go ahead");
    expect(synth.texts).toEqual(["I can check your AWS bill for last month. Shall I continue?", SERVER_SPEECH]);
    for (const t of synth.texts) expect(t).not.toMatch(/[{}[\]]/);
  });

  it("a result without a speech field is not read aloud as data", async () => {
    const { c, ask, synth } = setup({ result: {} });
    await ask("What was my AWS bill last month?");
    await ask("yes");
    expect(synth.texts.at(-1)).toBe("Sorry, that didn't work. The server returned no spoken answer.");
    expect(c.snapshot.status).toBe("speaking");
  });

  it("never overlaps: the previous utterance is cancelled first", async () => {
    const { c, ask, synth } = setup();
    await ask("What was my AWS bill last month?"); // prompt still speaking
    await ask("yes"); // listening stops the prompt; the result is spoken next
    c.replay();
    c.replay();
    expect(synth.overlaps).toBe(0);
    expect(synth.spoken.length).toBe(4);
  });

  it("a cancelled utterance's late end event does not change the state", async () => {
    const { c, ask, synth } = setup();
    await ask("What was my AWS bill last month?");
    const prompt = synth.spoken[0];
    c.replay(); // cancels the prompt, speaks it again
    prompt.onend?.(); // stale
    expect(c.snapshot.status).toBe("speaking");
    synth.finish();
    expect(c.snapshot.status).toBe("awaiting_confirmation");
  });

  it("replay repeats the last spoken text", async () => {
    const { c, ask, synth } = setup();
    await ask("What was my AWS bill last month?");
    await ask("yes");
    synth.finish();
    expect(c.snapshot.status).toBe("idle");
    c.replay();
    expect(synth.texts.at(-1)).toBe(SERVER_SPEECH);
    expect(c.snapshot.status).toBe("speaking");
  });

  it("stop speaking", async () => {
    const { c, ask, synth } = setup();
    await ask("What was my AWS bill last month?");
    expect(c.snapshot.status).toBe("speaking");
    c.stopSpeaking();
    expect(synth.active).toBeNull();
    expect(c.snapshot.status).toBe("awaiting_confirmation");
    expect(c.snapshot.pending).not.toBeNull();
  });

  it("mute stops speech, keeps the text, and unmute speaks again", async () => {
    const { c, ask, synth } = setup();
    await ask("What was my AWS bill last month?");
    c.setMuted(true);
    expect(synth.active).toBeNull();
    expect(c.snapshot).toMatchObject({ muted: true, status: "awaiting_confirmation" });
    const count = synth.spoken.length;
    await ask("yes");
    expect(synth.spoken.length).toBe(count); // nothing spoken while muted
    expect(c.snapshot.message).toBe(SERVER_SPEECH); // but the reply is shown
    expect(c.snapshot.status).toBe("idle");
    c.replay();
    expect(synth.spoken.length).toBe(count);
    c.setMuted(false);
    c.replay();
    expect(synth.texts.at(-1)).toBe(SERVER_SPEECH);
  });

  it("listening stops CloudGuard talking", async () => {
    const { c, ask, synth } = setup();
    await ask("What was my AWS bill last month?");
    expect(synth.active).not.toBeNull();
    c.startListening();
    expect(synth.active).toBeNull();
    expect(c.snapshot.status).toBe("listening");
  });

  it("works without speech synthesis", async () => {
    const { c, ask, execute } = setup({ synth: false });
    expect(c.snapshot.synthesisSupported).toBe(false);
    await ask("What was my AWS bill last month?");
    expect(c.snapshot.status).toBe("awaiting_confirmation");
    await ask("yes");
    expect(execute).toHaveBeenCalledTimes(1);
    expect(c.snapshot).toMatchObject({ status: "idle", message: SERVER_SPEECH });
  });
});

describe("confirmationOf", () => {
  it.each([["yes", "yes"], ["Yeah.", "yes"], ["confirm", "yes"], ["Go ahead!", "yes"], ["ok", "yes"],
    ["no", "no"], ["Cancel.", "no"], ["never mind", "no"], ["stop", "no"],
    ["yes please check last month", null], ["noted", null], ["go", null], ["", null], ["yes, no", "no"]])(
    "%j -> %s", (text, want) => {
      expect(confirmationOf(text)).toBe(want);
    });
});
