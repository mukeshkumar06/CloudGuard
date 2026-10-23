// Conversational bulk tagging by voice ("Tag all the Lambdas which have CodeBridge"): understand, search (read-only),
// ask for the tag (only the missing part), prepare on the server, read back, exact spoken confirmation, per-resource
// results. Pure steps (voicewrite.ts) and the voice controller with a fake operator API: no browser, no network,
// no AWS. The server-side binding (operator session, plan hash, expiry, replay) is tested in the backend
// (backend/tests/test_voice_tagging.py); here, what the browser does with each answer.
import { describe, expect, it, vi } from "vitest";

import type { ConfirmOut, OperatorApi, PrepareOut, PrepareRow } from "../src/lib/operator";
import { tagIntentIn } from "../src/lib/tagflow";
import { READ_ONLY_TOOLS } from "../src/lib/tools";
import { VoiceController, type VoiceDeps } from "../src/lib/voice";
import { onConfirm, onPrepare, onWriteAnswer, onWriteSearch, parseTagAnswer, startWrite, unchangedReason,
         type WriteFlow } from "../src/lib/voicewrite";

const ASK = "Tag all the Lambdas which have CodeBridge.";
const NAMES = ["codebridge-api", "codebridge-worker", "codebridge-cron"];
const search = (names = NAMES, complete = true) => ({ mode: "fixture", total: names.length, coverage_complete: complete,
                                                      resources: names.map((n) => ({ display_name: n })) });
const add = (name: string): PrepareRow => ({ name, region: "ap-south-1", outcome: "CHANGE_PROPOSED", current_value: { owner: null },
  changes: [{ key: "owner", action: "ADD", old_value: null, new_value: "Mukesh" }], conflicts: [] });
const caseConflict = (name: string): PrepareRow => ({ name, region: "ap-south-1", outcome: "CONFLICT", current_value: { owner: null },
  changes: [], conflicts: [{ key: "owner", existing_value: "payments-team", reason: "KEY_CASE_CONFLICT" }] });
const already = (name: string): PrepareRow => ({ name, region: "ap-south-1", outcome: "ALREADY_MATCHING",
  current_value: { owner: "Mukesh" }, changes: [], conflicts: [] });
const ready = (rows: PrepareRow[], id = "ch-1", extra: Partial<PrepareOut> = {}): PrepareOut => ({
  status: "READY", mode: "fixture", resources: rows, overwrites: [], requires_overwrite_ack: false,
  challenge: { id, expires_at: "2026-10-23T09:02:00Z", plan_hash: `sha256:${id}` }, ...extra });

function found(names = NAMES, complete = true): WriteFlow {
  const intent = tagIntentIn(ASK);
  expect(intent).toMatchObject({ service: "lambda", filter: { match: "contains", text: "CodeBridge" } });
  const start = startWrite(intent as never, 1, "ap-south-1");
  expect(start.call).toEqual({ kind: "search", args: { text: "CodeBridge", match: "contains", services: ["lambda"],
                                                       region: "ap-south-1", limit: 100 } });
  expect(start.call?.kind).toBe("search"); // the first step is a read-only search, never a write
  return onWriteSearch(start.flow, search(names, complete)).flow;
}
const withTag = (flow = found()) => onWriteAnswer(flow, "Key should be owner and value should be Mukesh").flow;

describe("1-3. understanding and searching", () => {
  it("1. 'Tag all the Lambdas which have CodeBridge' is a Lambda name-contains search", () => {
    const step = onWriteSearch(startWrite(tagIntentIn(ASK) as never, 1, "ap-south-1").flow, search());
    expect(step.flow.stage).toBe("need_tags");
    expect(step.call).toBeUndefined(); // nothing is prepared until the tag is known
  });

  it("2. the count and names are the server's, then the tag is asked for", () => {
    const step = onWriteSearch(startWrite(tagIntentIn(ASK) as never, 1, "ap-south-1").flow, search());
    expect(step.say).toContain("I found 3 Lambda functions matching CodeBridge in Asia Pacific (Mumbai)");
    for (const n of NAMES) expect(step.say).toContain(n);
    expect(step.say).toContain("What tag key and value would you like to apply?");
    expect(step.flow.found).toEqual({ total: 3, names: NAMES, complete: true });
  });

  it("3. zero matches ends the flow with nothing to tag", () => {
    const step = onWriteSearch(startWrite(tagIntentIn(ASK) as never, 1, "ap-south-1").flow, search([]));
    expect(step.say).toContain("I found no matching Lambda functions");
    expect(step.flow.stage).toBe("closed");
    expect(step.call).toBeUndefined();
  });

  it("11. incomplete discovery is disclosed before the tag is asked for", () => {
    const flow = found(NAMES, false);
    expect(flow.found?.complete).toBe(false);
    const step = onWriteSearch(startWrite(tagIntentIn(ASK) as never, 1, "ap-south-1").flow, search(NAMES, false));
    expect(step.say.indexOf("Discovery was incomplete")).toBeLessThan(step.say.indexOf("What tag key"));
  });
});

describe("4-7. collecting the tag", () => {
  it.each([
    ["Project is CodeBridge", { key: "Project", value: "CodeBridge" }],
    ["Use Environment as Development", { key: "Environment", value: "Development" }],
    ["Key should be Owner and value should be Mukesh", { key: "Owner", value: "Mukesh" }],
    ["owner=mukesh", { key: "owner", value: "mukesh" }],
  ])("natural answer %s", (said, tag) => {
    expect(parseTagAnswer(said)).toEqual({ tag });
    const step = onWriteAnswer(found(), said);
    expect(step.call).toMatchObject({ kind: "prepare", spec: { tags: { [tag.key]: tag.value } } });
  });

  it("4. neither key nor value: asks for both, prepares nothing", () => {
    for (const said of ["hmm", "that is fine", "it is okay", "yes"]) {
      const step = onWriteAnswer(found(), said);
      expect(step.call).toBeUndefined();
      expect(step.flow.stage).toBe("need_tags");
      expect(step.say).toContain("What tag key and value");
    }
  });

  it("5. only the value said: asks only for the key, then combines", () => {
    const half = onWriteAnswer(found(), "value is Mukesh");
    expect(half.call).toBeUndefined();
    expect(half.say).toBe("Okay, the value is Mukesh. Which tag key should have that value?");
    const step = onWriteAnswer(half.flow, "Owner");
    expect(step.call).toMatchObject({ kind: "prepare", spec: { tags: { Owner: "Mukesh" } } });
  });

  it("6. only the key said: asks only for the value, then combines", () => {
    const half = onWriteAnswer(found(), "key is Owner");
    expect(half.say).toBe("Okay, the key is Owner. What value should Owner have?");
    expect(half.flow.partial).toEqual({ key: "Owner" });
    const step = onWriteAnswer(half.flow, "Mukesh Kumar");
    expect(step.call).toMatchObject({ kind: "prepare", spec: { tags: { Owner: "Mukesh Kumar" } } });
  });

  it("7. multi-turn: the controller keeps service, filter, region and results across turns", async () => {
    const api = { prepare: vi.fn<OperatorApi["prepare"]>(async () => ready(NAMES.map(add))),
                  confirm: vi.fn<OperatorApi["confirm"]>(async () => ({ status: "EXECUTED", outcome: "SUCCEEDED", results: [] } as ConfirmOut)),
                  cancel: vi.fn<OperatorApi["cancel"]>(async () => {}) };
    const call = vi.fn<NonNullable<VoiceDeps["call"]>>(async () => ({ structured: search() }));
    const c = new VoiceController({ createRecognition: null, synth: null, createUtterance: null,
      tools: () => [...READ_ONLY_TOOLS, "preview_tag_changes"], needsConfirmation: () => true, execute: vi.fn(), call,
      selectedRegion: () => "ap-south-1", operator: () => api }, undefined, { autoListen: false });
    const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0)); };
    await c.handleTranscript(ASK);
    expect(call).not.toHaveBeenCalled(); // read back as heard first
    await c.handleTranscript("yes");
    expect(call).not.toHaveBeenCalled(); // then: which region?
    await c.handleTranscript("all regions"); // tagging is one region at a time: asked again, nothing runs
    expect(call).not.toHaveBeenCalled();
    expect(c.snapshot.message).toMatch(/one region at a time/);
    await c.handleTranscript("Mumbai");
    await tick();
    expect(call).toHaveBeenCalledTimes(1);
    expect(call).toHaveBeenCalledWith("search_resources", expect.objectContaining({ region: "ap-south-1" }));
    await c.handleTranscript("key is owner");
    await tick();
    expect(api.prepare).not.toHaveBeenCalled();
    expect(c.snapshot.writeFlow).toMatchObject({ service: "lambda", region: "ap-south-1",
      filter: { text: "CodeBridge" }, found: { total: 3 }, partial: { key: "owner" } });
    await c.handleTranscript("Mukesh");
    await tick();
    expect(api.prepare).toHaveBeenCalledWith({ service: "lambda", region: "ap-south-1", match: "contains",
                                               text: "CodeBridge", tags: { owner: "Mukesh" } });
    expect(api.confirm).not.toHaveBeenCalled(); // a preview is not a write
    await c.handleTranscript("sure thing"); // a generic answer is not the exact confirmation
    await tick();
    expect(api.confirm).not.toHaveBeenCalled();
    await c.handleTranscript("yes");
    await tick();
    expect(api.confirm).toHaveBeenCalledTimes(1);
    expect(api.confirm).toHaveBeenCalledWith("ch-1", "yes", undefined);
  });
});

describe("8-12. the server's preview", () => {
  it("12. the read-back lists counts, names, key and value, and asks for yes or no", () => {
    const step = onPrepare(withTag(), ready([add("codebridge-api"), already("codebridge-worker"), caseConflict("codebridge-cron")]));
    for (const part of ["Matched: 3 Lambda functions", "To change: 1", "already correct: codebridge-worker",
                        "Tag key: owner", "Tag value: Mukesh", "Say yes or no"]) expect(step.say).toContain(part);
    expect(step.flow).toMatchObject({ stage: "confirm", challenge: "ch-1" });
  });

  it("8. resources that already have the tag are counted as already correct, not changed", () => {
    const step = onPrepare(withTag(), ready([already("codebridge-api"), add("codebridge-worker")]));
    expect(step.say).toContain("To change: 1; already correct: codebridge-api");
  });

  it("9. a key differing only in case is explained per resource, and never added as a second key", () => {
    const row = caseConflict("codebridge-cron");
    expect(unchangedReason(row)).toContain("different capitalisation");
    expect(unchangedReason(row)).toContain("case-sensitive");
    const step = onPrepare(withTag(), ready([add("codebridge-api"), row]));
    expect(step.say).toContain("Left unchanged: codebridge-cron, it already has owner with different capitalisation");
  });

  it("10. an overwrite is never silent: listed, then a separate 'I confirm the overwrite'", () => {
    const ow = { ...add("codebridge-api"), changes: [{ key: "owner", action: "OVERWRITE", old_value: "old", new_value: "Mukesh" }] };
    const flow = onPrepare(withTag(), ready([ow], "ch-1", { requires_overwrite_ack: true,
      overwrites: [{ name: "codebridge-api", key: "owner", old_value: "old", new_value: "Mukesh" }] })).flow;
    expect(flow.question).toContain("This overwrites existing values: codebridge-api, owner from old to Mukesh");
    const yes = onWriteAnswer(flow, "yes");
    expect(yes.call).toBeUndefined();
    expect(yes.flow.stage).toBe("overwrite_ack");
    expect(onWriteAnswer(yes.flow, "yes").call).toBeUndefined(); // a second plain yes is not the phrase
    expect(onWriteAnswer(yes.flow, "I confirm the overwrite").call)
      .toEqual({ kind: "confirm", challenge: "ch-1", transcript: "yes", overwrite: "I confirm the overwrite" });
  });

  it("a conflict without an overwrite request stops; adding never overwrites", () => {
    const step = onPrepare(withTag(), { status: "NO_CHANGES", mode: "fixture", speech: "codebridge-api has owner equals old.",
      resources: [{ ...add("codebridge-api"), changes: [], conflicts: [{ key: "owner", existing_value: "old", reason: "WOULD_OVERWRITE" }] }] });
    expect(step.flow.stage).toBe("conflict");
    expect(step.call).toBeUndefined();
  });
});

describe("13-15. confirmation and results", () => {
  const confirmFlow = () => onPrepare(withTag(), ready(NAMES.map(add))).flow;

  it("13. only an exact spoken yes confirms; a changed tag voids the challenge", () => {
    const flow = confirmFlow();
    for (const said of ["sounds good", "maybe", "yes but later", "that's fine", "sure thing"]) expect(onWriteAnswer(flow, said).call).toBeUndefined();
    expect(onWriteAnswer(flow, "yes").call).toEqual({ kind: "confirm", challenge: "ch-1", transcript: "yes" });
    const changed = onWriteAnswer(flow, "No, key is team, value is core.");
    expect(changed.cancel).toBe("ch-1");
    expect(changed.call).toMatchObject({ kind: "prepare", spec: { tags: { team: "core" } } });
  });

  it("14. an expired or changed challenge is never retried with the old yes: a new plan is read back", () => {
    for (const error of ["CHALLENGE_EXPIRED", "PROPOSAL_CHANGED"]) {
      const step = onConfirm({ ...confirmFlow(), stage: "executing" }, { status: "REFUSED", error, speech: "That confirmation expired." });
      expect(step.call?.kind).toBe("prepare");
      expect(step.flow.challenge).toBeUndefined();
    }
    // A replayed answer: the flow is done; any further "yes" does nothing.
    const done = onConfirm({ ...confirmFlow(), stage: "executing" }, { status: "EXECUTED", replayed: true, results: [] });
    expect(onWriteAnswer(done.flow, "yes").call).toBeUndefined();
  });

  it("15. per-resource results come from the server, including a failed verification", () => {
    const out: ConfirmOut = { status: "EXECUTED", outcome: "PARTIAL", speech: "Tagged and verified 1. 1 failed verification.",
      results: [
        { name: "codebridge-api", category: "tagged", status: "APPLIED_VERIFIED", error: "NONE", revalidation: "ELIGIBLE", detail: "" },
        { name: "codebridge-worker", category: "already_correct", status: "ALREADY_APPLIED", error: "NONE", revalidation: "ELIGIBLE", detail: "" },
        { name: "codebridge-cron", category: "failed", status: "FAILED_VERIFICATION", error: "VERIFICATION", revalidation: "ELIGIBLE", detail: "" },
      ] };
    const step = onConfirm({ ...confirmFlow(), stage: "executing" }, out);
    expect(step.flow).toMatchObject({ stage: "done", outcome: "PARTIAL" });
    expect(step.flow.results?.map((r) => [r.name, r.category])).toEqual([
      ["codebridge-api", "tagged"], ["codebridge-worker", "already_correct"], ["codebridge-cron", "failed"]]);
    expect(step.say).toBe(out.speech); // the server's words, never a success the server didn't report
  });
});
