// Personal voice tagging in the browser (voicewrite.ts + the voice controller): the steps, and that only an explicit
// spoken yes for the current plan reaches the server's confirm. A fake operator API and fake tools: no browser, no
// network, no AWS.
import { describe, expect, it, vi } from "vitest";

import type { ConfirmOut, OperatorApi, PrepareOut } from "../src/lib/operator";
import { tagIntentIn } from "../src/lib/tagflow";
import { READ_ONLY_TOOLS } from "../src/lib/tools";
import { VoiceController, type VoiceDeps } from "../src/lib/voice";
import { onConfirm, onPrepare, onWriteAnswer, onWriteSearch, startWrite, type WriteFlow } from "../src/lib/voicewrite";

const TOOLS = [...READ_ONLY_TOOLS, "preview_tag_changes"];
const ASK = "Find all Lambda functions containing CodeBridge in Mumbai and tag them.";
const NAMES = ["codebridge-api", "codebridge-worker"];
const SEARCH = { mode: "live", total: 2, resources: NAMES.map((n) => ({ display_name: n })) };
const row = (name: string, extra = {}) => ({ name, region: "ap-south-1", outcome: "CHANGE_PROPOSED",
  changes: [{ key: "owner", action: "ADD", old_value: null, new_value: "mukesh" }], conflicts: [], ...extra });
const READY = (id = "ch-1", extra: Partial<PrepareOut> = {}): PrepareOut => ({
  status: "READY", mode: "live", resources: NAMES.map((n) => row(n)), overwrites: [], requires_overwrite_ack: false,
  challenge: { id, expires_at: "2026-10-23T09:02:00Z", plan_hash: "sha256:x" }, ...extra });
const EXECUTED: ConfirmOut = { status: "EXECUTED", speech: "Tagged and verified 2: codebridge-api and codebridge-worker.",
  results: NAMES.map((name) => ({ name, category: "tagged", status: "APPLIED_VERIFIED", error: "NONE",
                                  revalidation: "ELIGIBLE", detail: "" })) };

function searched(): WriteFlow {
  const start = startWrite(tagIntentIn(ASK) as never, 1, "us-east-1");
  expect(start.flow.region).toBe("ap-south-1"); // named in the request: it wins over the selected region
  expect(start.call).toEqual({ kind: "search", args: { text: "CodeBridge", match: "contains", services: ["lambda"],
                                                       region: "ap-south-1", limit: 100 } });
  return onWriteSearch(start.flow, SEARCH).flow;
}

describe("steps", () => {
  it("reports count and names from live discovery, then asks for the tag", () => {
    const step = onWriteSearch(startWrite(tagIntentIn(ASK) as never, 1, "ap-south-1").flow, SEARCH);
    expect(step.say).toContain("From live discovery of your AWS account: I found 2 Lambda functions");
    expect(step.say).toContain("codebridge-api and codebridge-worker");
    expect(step.flow.stage).toBe("need_tags");
  });

  it.each(["Key is owner, value is mukesh.", "Use owner=mukesh.", "owner=mukesh"])("%s prepares on the server", (said) => {
    const step = onWriteAnswer(searched(), said);
    expect(step.call).toEqual({ kind: "prepare", spec: { service: "lambda", region: "ap-south-1", match: "contains",
                                                         text: "CodeBridge", tags: { owner: "mukesh" } } });
  });

  it("repeats the complete operation from the server's plan", () => {
    const flow = onWriteAnswer(searched(), "Key is owner, value is mukesh.").flow;
    const step = onPrepare(flow, READY());
    for (const part of ["Service: Lambda functions", "Region: Asia Pacific (Mumbai), ap-south-1",
                        "Name filter: names containing CodeBridge", "Matched: 2 Lambda functions",
                        "codebridge-api and codebridge-worker", "Tag key: owner", "Tag value: mukesh",
                        "Say yes or no"]) expect(step.say).toContain(part);
    expect(step.flow).toMatchObject({ stage: "confirm", challenge: "ch-1" });
  });

  it("only an explicit yes confirms; unclear answers repeat the question", () => {
    const flow = onPrepare(onWriteAnswer(searched(), "Key is owner, value is mukesh.").flow, READY()).flow;
    for (const said of ["sounds good", "that's fine", "maybe", "yes, but later", ""]) {
      const step = onWriteAnswer(flow, said);
      expect(step.call).toBeUndefined();
      expect(step.flow.stage).toBe("confirm");
    }
    expect(onWriteAnswer(flow, "yes").call).toEqual({ kind: "confirm", challenge: "ch-1", transcript: "yes" });
  });

  it("no cancels the challenge and asks for a corrected tag", () => {
    const flow = onPrepare(onWriteAnswer(searched(), "Key is owner, value is mukesh.").flow, READY()).flow;
    const no = onWriteAnswer(flow, "no");
    expect(no).toMatchObject({ cancel: "ch-1", flow: { stage: "need_tags" } });
    expect(no.call).toBeUndefined();
    expect(no.say).toContain("Nothing was written");
  });

  it("a corrected tag voids the old challenge and prepares a new plan", () => {
    const flow = onPrepare(onWriteAnswer(searched(), "Key is owner, value is mukesh.").flow, READY()).flow;
    const changed = onWriteAnswer(flow, "No, key is dev, value is mukesh.");
    expect(changed.cancel).toBe("ch-1");
    expect(changed.call).toMatchObject({ kind: "prepare", spec: { tags: { dev: "mukesh" } } });
    expect(changed.flow.challenge).toBeUndefined();
  });

  it("overwrites need a distinct acknowledgement after the yes", () => {
    const flow = onWriteAnswer(searched(), "Key is owner, value is mukesh.").flow;
    const ready = onPrepare(flow, READY("ch-2", { requires_overwrite_ack: true,
      overwrites: [{ name: "codebridge-api", key: "owner", old_value: "bob", new_value: "mukesh" }] }));
    expect(ready.say).toContain("codebridge-api, owner from bob to mukesh");
    const yes = onWriteAnswer(ready.flow, "yes");
    expect(yes.call).toBeUndefined();
    expect(yes.flow.stage).toBe("overwrite_ack");
    expect(onWriteAnswer(yes.flow, "yes").call).toBeUndefined();
    expect(onWriteAnswer(yes.flow, "I confirm the overwrite.").call).toEqual(
      { kind: "confirm", challenge: "ch-2", transcript: "yes", overwrite: "I confirm the overwrite." });
  });

  it("an existing different value offers an overwrite, which is planned again on the server", () => {
    const flow = onWriteAnswer(searched(), "Key is owner, value is mukesh.").flow;
    const conflict = onPrepare(flow, { status: "NO_CHANGES", speech: "No tag would change.", challenge: null,
      resources: [row("codebridge-api", { changes: [], outcome: "CONFLICT",
                                          conflicts: [{ key: "owner", existing_value: "bob", reason: "WOULD_OVERWRITE" }] })] });
    expect(conflict.flow.stage).toBe("conflict");
    expect(onWriteAnswer(conflict.flow, "overwrite them").call).toMatchObject({ kind: "prepare",
      spec: { overwrite: true } });
  });

  it("an expired or changed confirmation is never retried: the plan is prepared and confirmed again", () => {
    const flow = onWriteAnswer(onPrepare(onWriteAnswer(searched(), "Key is owner, value is mukesh.").flow, READY()).flow,
                               "yes").flow;
    for (const error of ["CHALLENGE_EXPIRED", "PROPOSAL_CHANGED"]) {
      const step = onConfirm(flow, { error, speech: "Nothing was written." });
      expect(step.call?.kind).toBe("prepare");
    }
  });

  it("reports the server's per-resource results", () => {
    const flow = onWriteAnswer(onPrepare(onWriteAnswer(searched(), "Key is owner, value is mukesh.").flow, READY()).flow,
                               "yes").flow;
    const done = onConfirm(flow, EXECUTED);
    expect(done.flow.stage).toBe("done");
    expect(done.say).toBe(EXECUTED.speech);
  });
});

describe("voice controller", () => {
  function setup(operatorActive = true) {
    const api = { prepare: vi.fn<OperatorApi["prepare"]>(async () => READY()),
                  confirm: vi.fn<OperatorApi["confirm"]>(async () => EXECUTED),
                  cancel: vi.fn<OperatorApi["cancel"]>(async () => {}) };
    const call = vi.fn<NonNullable<VoiceDeps["call"]>>(async (tool) => ({
      structured: tool === "search_resources" ? SEARCH : { status: "PROPOSAL_READY", request: {} } }));
    const c = new VoiceController({ createRecognition: null, synth: null, createUtterance: null, tools: () => TOOLS,
                                    needsConfirmation: () => true, execute: vi.fn(), call,
                                    selectedRegion: () => "ap-south-1",
                                    operator: () => (operatorActive ? api : null) }, undefined, { autoListen: false });
    return { c, api, call };
  }
  const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0)); };

  it("collects, repeats, confirms once and reports", async () => {
    const { c, api } = setup();
    await c.handleTranscript(ASK);
    await c.handleTranscript("yes"); // "I heard: ... Is that right?"
    await tick();
    expect(c.snapshot.message).toContain("I found 2 Lambda functions");
    await c.handleTranscript("Key is owner, value is mukesh.");
    await tick();
    expect(api.prepare).toHaveBeenCalledTimes(1);
    expect(c.snapshot.message).toContain("Tag value: mukesh");
    await c.handleTranscript("sounds good");
    await tick();
    expect(api.confirm).not.toHaveBeenCalled();
    await c.handleTranscript("No, key is dev, value is mukesh.");
    await tick();
    expect(api.cancel).toHaveBeenCalledWith("ch-1");
    expect(api.prepare).toHaveBeenCalledTimes(2);
    await c.handleTranscript("yes");
    await tick();
    expect(api.confirm).toHaveBeenCalledTimes(1);
    expect(c.snapshot.message).toContain("Tagged and verified 2");
  });

  it("a duplicate final transcript never confirms twice", async () => {
    const { c, api } = setup();
    let release!: () => void;
    api.confirm.mockImplementation(() => new Promise((r) => { release = () => r(EXECUTED); }));
    await c.handleTranscript(ASK);
    await c.handleTranscript("yes"); // "I heard: ... Is that right?"
    await tick();
    await c.handleTranscript("Key is owner, value is mukesh.");
    await tick();
    void c.handleTranscript("yes");
    await tick();
    await c.handleTranscript("yes"); // the recognizer resends the same final result while confirm runs
    await c.handleTranscript("yes");
    release();
    await tick();
    await c.handleTranscript("yes"); // and again after it finished
    await tick();
    expect(api.confirm).toHaveBeenCalledTimes(1);
  });

  it("silence and partial transcripts send nothing", async () => {
    const { c, api } = setup();
    await c.handleTranscript(ASK);
    await c.handleTranscript("yes"); // "I heard: ... Is that right?"
    await tick();
    await c.handleTranscript("Key is owner, value is mukesh.");
    await tick();
    // No final transcript arrives (silence), or only interim words were heard: handleTranscript is never called
    // for those, so nothing is confirmed. A truncated final ("ye") is unclear.
    await c.handleTranscript("ye");
    await tick();
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it("cancel and disconnect void the pending challenge", async () => {
    const { c, api } = setup();
    await c.handleTranscript(ASK);
    await c.handleTranscript("yes"); // "I heard: ... Is that right?"
    await tick();
    await c.handleTranscript("Key is owner, value is mukesh.");
    await tick();
    c.reset(); // disconnect
    expect(api.cancel).toHaveBeenCalledWith("ch-1");
    expect(c.snapshot.writeFlow).toBeNull();
    await c.handleTranscript("yes");
    await tick();
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it("without an operator session, tagging stays preview-only", async () => {
    const { c, api, call } = setup(false);
    await c.handleTranscript(ASK);
    await c.handleTranscript("yes"); // "I heard: ... Is that right?"
    await tick();
    await c.handleTranscript("Key is owner, value is mukesh.");
    await c.handleTranscript("yes");
    await tick();
    expect(api.prepare).not.toHaveBeenCalled();
    expect(api.confirm).not.toHaveBeenCalled();
    expect(call.mock.calls.map(([t]) => t)).toEqual(["search_resources", "preview_tag_changes"]);
  });
});
