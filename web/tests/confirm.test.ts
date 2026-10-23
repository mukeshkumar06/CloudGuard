// Spoken confirmation parsing, and how the controller acts on it. Fake speech APIs; `execute` is a spy.
import { describe, expect, it, vi } from "vitest";

import { confirmationOf, normalizeUtterance, parseConfirmation } from "../src/lib/confirm";
import {
  CANCELLED_TEXT, REPEAT_TEXT, VoiceController, type ExecuteResult, type RecognitionLike, type SynthLike,
  type UtteranceLike, type VoiceDeps,
} from "../src/lib/voice";

describe("parseConfirmation: yes", () => {
  it.each(["yes", "yes yes", "yeah", "yep", "sure", "absolutely", "okay", "ok", "go ahead", "continue", "proceed",
    "certainly", "do it", "confirm", "confirmed", "yes please", "please do", "of course", "alright"])(
    "%j -> yes", (t) => expect(parseConfirmation(t)).toEqual({ answer: "yes" }));

  it.each(["Yes.", "YES!", "Yes, yes.", "yes... yes", "  Yeah  ", "Okay, go ahead.", "OK!", "O.K.", "o.k", "Go-ahead",
    "Sure, thanks!", "Yes, please continue.", "yes yes yes", "yeah yeah", "ok ok", "Yesss", "Absolutely, do it.",
    "Yes CloudGuard", "Go ahead then", "yes, do it now"])(
    "case, punctuation and recognizer variations: %j -> yes", (t) => expect(confirmationOf(t)).toBe("yes"));
});

describe("parseConfirmation: no", () => {
  it.each(["no", "nope", "no thanks", "No, thank you.", "cancel", "Cancel that.", "stop", "don't", "dont",
    "do not", "not now", "Not now, thanks", "never mind", "nevermind", "no no", "Nooo", "nah", "abort"])(
    "%j -> no", (t) => expect(parseConfirmation(t)).toEqual({ answer: "no" }));

  it("a curly apostrophe from the recognizer still reads as don't", () => {
    expect(parseConfirmation("Don’t")).toEqual({ answer: "no" });
  });
});

describe("parseConfirmation: qualified, mixed and contradictory", () => {
  it.each(["yes, but check this month instead", "yes please check last month", "yes and also compare costs",
    "okay but only for September", "what about compliance", "maybe", "I guess", "hmm", "go", "", "   ",
    "yesterday", "noted", "the bill", "can you go ahead and check compliance instead"])(
    "%j -> ambiguous (nothing runs)", (t) => expect(parseConfirmation(t).answer).toBe("ambiguous"));

  it.each(["yes, but don't run it", "yes, no", "yes no", "okay wait", "yes... actually no", "sure, cancel that",
    "go ahead, no, stop", "yes but not now"])(
    "contradictory %j -> no (a refusal always wins)", (t) => expect(parseConfirmation(t).answer).toBe("no"));

  it("a refusal followed by a request carries the request as `rest`", () => {
    expect(parseConfirmation("No, but show me the account.")).toEqual({ answer: "no", rest: "show me the account" });
    expect(parseConfirmation("Cancel that and check my tagging compliance")).toEqual(
      { answer: "no", rest: "check my tagging compliance" });
    expect(parseConfirmation("yes, no, which account am I using")).toEqual({ answer: "no" }); // mixed: no new question
  });

  it("normalizes text", () => {
    expect(normalizeUtterance("  Yes,   YES!  O.K. Go-ahead… ")).toBe("yes yes ok go ahead");
  });
});

// ---------- controller ----------

class Rec implements RecognitionLike {
  lang = ""; interimResults = false; continuous = true; maxAlternatives = 0;
  onresult: RecognitionLike["onresult"] = null;
  onerror: RecognitionLike["onerror"] = null;
  onend: RecognitionLike["onend"] = null;
  aborted = 0;
  onstart: RecognitionLike["onstart"] = null;
  start() { this.onstart?.(); }
  stop() { this.onend?.(); }
  abort() { this.aborted++; this.onerror?.({ error: "aborted" }); this.onend?.(); }
  hear(text: string) {
    this.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: true })] });
    this.onend?.();
  }
}

class Synth implements SynthLike {
  spoken: UtteranceLike[] = [];
  active: UtteranceLike | null = null;
  speak(u: UtteranceLike) { this.active = u; this.spoken.push(u); }
  cancel() { const u = this.active; this.active = null; u?.onerror?.(); }
  get texts() { return this.spoken.map((u) => u.text); }
}

const TOOLS = ["get_monthly_aws_bill", "scan_tag_compliance", "get_aws_account_identity"];
const PROMPT = "I can check your AWS bill for last month. Shall I continue?";

function setup(result: () => Promise<ExecuteResult> = async () => ({ speech: "Done." })) {
  const recs: Rec[] = [];
  const synth = new Synth();
  const execute = vi.fn<VoiceDeps["execute"]>(result);
  const c = new VoiceController({
    createRecognition: () => { const r = new Rec(); recs.push(r); return r; },
    synth, createUtterance: (text) => ({ text, lang: "", onend: null, onerror: null }),
    tools: () => TOOLS, execute,
  }, undefined, { autoListen: false });
  const flush = () => new Promise((r) => setTimeout(r, 0));
  const say = async (text: string) => { c.startListening(); recs[recs.length - 1].hear(text); await flush(); };
  return { c, recs, synth, execute, say, flush };
}

describe("controller confirmation", () => {
  it.each(["yes yes", "Yes, yes.", "yeah", "yep", "sure", "absolutely", "okay", "go ahead", "continue", "proceed"])(
    "%j runs the pending call exactly once", async (word) => {
      const { c, execute, say } = setup();
      await say("How much did AWS cost last month?");
      await say(word);
      expect(execute).toHaveBeenCalledTimes(1);
      expect(execute).toHaveBeenCalledWith("get_monthly_aws_bill", { month: "last-month" });
      expect(c.snapshot.pending).toBeNull();
    });

  it.each(["no", "no thanks", "nope", "cancel", "stop", "don't", "do not", "not now", "yes, but don't run it"])(
    "%j cancels and runs nothing", async (word) => {
      const { c, synth, execute, say } = setup();
      await say("How much did AWS cost last month?");
      await say(word);
      expect(execute).not.toHaveBeenCalled();
      expect(c.snapshot.pending).toBeNull();
      expect(synth.texts.at(-1)).toBe(CANCELLED_TEXT);
    });

  it.each(["yes, but check this month instead", "yes and compare with August", "maybe"])(
    "%j keeps the call pending, runs nothing and asks again", async (word) => {
      const { c, synth, execute, say } = setup();
      await say("How much did AWS cost last month?");
      const pending = c.snapshot.pending;
      await say(word);
      expect(execute).not.toHaveBeenCalled();
      expect(c.snapshot.pending).toBe(pending);
      expect(synth.texts.at(-1)).toBe(`${REPEAT_TEXT} ${PROMPT}`);
    });

  it("\"no, but show me the account\" cancels; the rest is matched as a new question (no tool for that wording)", async () => {
    const { c, synth, execute, say } = setup();
    await say("How much did AWS cost last month?");
    await say("No, but show me the account.");
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.pending).toBeNull();
    expect(synth.texts.at(-1)).toMatch(/^Okay, cancelled\. Nothing was run\. No tool matches that question/);
  });

  it("\"no, which AWS account am I using\" cancels first, then asks to confirm the new question", async () => {
    const { c, synth, execute, say } = setup();
    await say("How much did AWS cost last month?");
    const old = c.snapshot.pending!;
    await say("No, but which AWS account am I using?");
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.pending).toMatchObject({ tool: "get_aws_account_identity" });
    expect(c.snapshot.pending!.id).not.toBe(old.id);
    expect(synth.texts.at(-1)).toBe(`${CANCELLED_TEXT} I can check which AWS account CloudGuard is using. Shall I continue?`);
    await say("yes");
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith("get_aws_account_identity", {});
  });

  it("\"no, but delete my buckets\" cancels and the write request is refused, not run", async () => {
    const { c, synth, execute, say } = setup();
    await say("How much did AWS cost last month?");
    await say("No, but delete my buckets");
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.pending).toBeNull();
    expect(synth.texts.at(-1)).toMatch(/^Okay, cancelled\. Nothing was run\. CloudGuard is read-only/);
  });

  it("double confirmation runs once: yes yes, then yes again, then the button", async () => {
    let resolve!: (r: ExecuteResult) => void;
    const { c, execute, say, flush } = setup(() => new Promise((r) => { resolve = r; }));
    await say("How much did AWS cost last month?");
    const id = c.snapshot.pending!.id;
    await say("yes yes");
    expect(c.snapshot.status).toBe("executing");
    c.startListening(); // the microphone is unavailable while executing
    await c.confirm(id); // a button click for the same call
    resolve({ speech: "Done." });
    await flush();
    await say("yes"); // nothing is pending any more
    await c.confirm(id);
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("button and voice use the same guarded path: a voice yes after a button yes does nothing", async () => {
    const { c, recs, execute, say, flush } = setup();
    await say("How much did AWS cost last month?");
    c.startListening();
    const session = recs.at(-1)!;
    await c.confirm(c.snapshot.pending!.id); // the button
    session.hear("yes yes"); // late words from the session the button aborted
    await flush();
    expect(session.aborted).toBe(1);
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("a stale recognition event cannot confirm a cancelled call", async () => {
    const { c, recs, execute, say, flush } = setup();
    await say("How much did AWS cost last month?");
    c.startListening();
    const session = recs.at(-1)!;
    c.cancel(); // button cancel aborts the session
    session.hear("yes");
    await flush();
    expect(execute).not.toHaveBeenCalled();
  });

  it("a stale recognition event cannot confirm a replaced call", async () => {
    const { c, recs, execute, say, flush } = setup();
    await say("How much did AWS cost last month?");
    c.startListening();
    const session = recs.at(-1)!;
    const internal = c as unknown as { state: { pending: { id: number } | null } };
    internal.state = { ...internal.state, pending: { ...internal.state.pending!, id: 4242 } }; // replaced underneath
    session.hear("yes yes");
    await flush();
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.pending).toMatchObject({ id: 4242 });
  });

  it("confirmation after cancellation does nothing", async () => {
    const { c, execute, say } = setup();
    await say("How much did AWS cost last month?");
    const id = c.snapshot.pending!.id;
    await say("cancel");
    await say("yes yes");
    await c.confirm(id);
    expect(execute).not.toHaveBeenCalled();
  });
});
