// Conversational group tagging (tagflow.ts): parsing, the search -> tag -> confirm -> preview steps, and the voice
// controller. Only read-only tools are ever called, and only an explicit yes to the current summary reaches the
// preview. Fake tools and speech: no browser, no network, no AWS.
import { describe, expect, it, vi } from "vitest";

import { TAG_CHANGE_REFUSAL, mapIntent } from "../src/lib/intent";
import { onAnswer, onPreviewResult, onSearchResult, parseTagKeyValue, startTagFlow, summary, tagIntentIn,
         type TagFlow } from "../src/lib/tagflow";
import { READ_ONLY_TOOLS } from "../src/lib/tools";
import { VoiceController, type VoiceDeps } from "../src/lib/voice";

const TOOLS = [...READ_ONLY_TOOLS, "preview_tag_changes"];
const ASK = "Find all Lambdas with codebridge in their names and tag them.";
const NAMES = ["codebridge-api", "codebridge-worker", "codebridge-cron"];
const SEARCH = { mode: "live", total: 3, coverage_complete: true,
                 resources: NAMES.map((n) => ({ display_name: n, name_withheld: false })) };
const REQUEST = { operation: "BULK_ADD_TAG", target: { resource_type: "AWS::Lambda::Function", region: "ap-south-1",
                  all_of_type: true, name_filter: { match: "contains", text: "codebridge" } },
                  tags: { owner: "mukesh" }, raw_text: "preview_tag_changes", parsed_by: "mcp" };
const READY = { status: "PROPOSAL_READY", matched: 3, changing: 3, conflicts: 0, request: REQUEST,
                speech: "The preview matches 3 resources, and 3 would change. Nothing was changed or stored." };

describe("parsing", () => {
  it("detects service, name filter and (default) region", () => {
    expect(tagIntentIn(ASK)).toEqual({ service: "lambda", filter: { match: "contains", text: "codebridge" } });
    expect(tagIntentIn("Tag the Lambda functions containing codebridge in Singapore")).toMatchObject(
      { service: "lambda", region: "ap-southeast-1" });
    expect(tagIntentIn("Tag all Lambdas starting with cb with owner=mukesh")).toMatchObject(
      { filter: { match: "starts_with", text: "cb" }, tag: { key: "owner", value: "mukesh" } });
  });

  it("maps to the tagging conversation only when the server lists both read-only tools", () => {
    expect(mapIntent(ASK, TOOLS)).toMatchObject({ kind: "tag" });
    expect(mapIntent(ASK, [...READ_ONLY_TOOLS])).toEqual({ kind: "none", reason: TAG_CHANGE_REFUSAL });
    expect(mapIntent("Tag all Lambdas containing x in all regions", TOOLS)).toMatchObject({ kind: "none" });
    expect(mapIntent("Show me untagged resources.", TOOLS)).toMatchObject({ kind: "needs", tool: "list_untagged_resources",
                                                                           missing: "region" });
    expect(mapIntent("Apply the approved changes.", TOOLS)).toEqual({ kind: "none", reason: TAG_CHANGE_REFUSAL });
  });

  it.each([
    ["Key is owner, value is mukesh.", "owner", "mukesh"],
    ["No, key is dev, value is mukesh.", "dev", "mukesh"],
    ["key is owner value is Mukesh Kumar", "owner", "Mukesh Kumar"],
    ["The key should be team and the value should be platform", "team", "platform"],
    ["value is mukesh, key is owner", "owner", "mukesh"],
    ["owner=mukesh", "owner", "mukesh"],
  ])("%s", (text, key, value) => {
    expect(parseTagKeyValue(text)).toEqual({ key, value });
  });

  it.each(["yes", "sounds good", "owner", "mukesh", ""])("%s is not a key and value", (text) => {
    expect(parseTagKeyValue(text)).toBeUndefined();
  });
});

function searched(): TagFlow {
  const start = startTagFlow(tagIntentIn(ASK) as never, 1, "ap-south-1");
  expect(start.call).toEqual({ tool: "search_resources", args: { text: "codebridge", match: "contains",
    services: ["lambda"], region: "ap-south-1", limit: 100 } });
  return onSearchResult(start.flow, SEARCH).flow;
}

describe("steps", () => {
  it("reports the count and names, then asks for the tag", () => {
    const start = startTagFlow(tagIntentIn(ASK) as never, 1, "ap-south-1");
    const step = onSearchResult(start.flow, SEARCH);
    expect(step.flow.stage).toBe("need_tags");
    expect(step.say).toContain("From live discovery of your AWS account: I found 3 Lambda functions: codebridge-api, "
      + "codebridge-worker and codebridge-cron");
    expect(step.say).toContain("What tag key and value");
  });

  it("never claims live discovery for fixture data, and stops on no matches", () => {
    const start = startTagFlow(tagIntentIn(ASK) as never, 1, "ap-south-1");
    expect(onSearchResult(start.flow, { ...SEARCH, mode: "fixture" }).say).toMatch(/^Fixture mode, synthetic demo data/);
    expect(onSearchResult(start.flow, { ...SEARCH, mode: "fixture" }).say).not.toContain("live");
    const none = onSearchResult(start.flow, { ...SEARCH, total: 0, resources: [] });
    expect(none.flow.stage).toBe("closed");
    expect(none.say).toContain("Nothing was written");
  });

  it("repeats the whole operation, and only an explicit yes reaches the preview", () => {
    const asked = onAnswer(searched(), "Key is owner, value is mukesh.");
    expect(asked.call).toBeUndefined();
    expect(asked.say).toBe(summary(asked.flow));
    for (const part of ["Service: Lambda functions", "Region: Asia Pacific (Mumbai), ap-south-1",
                        "Name filter: names containing codebridge", "Matched: 3 Lambda functions", "Tag key: owner",
                        "Tag value: mukesh", "Say yes or no"]) expect(asked.say).toContain(part);
    for (const unclear of ["sounds good", "that's fine", "yes, but later", "maybe", "hmm"]) {
      const step = onAnswer(asked.flow, unclear);
      expect(step.call).toBeUndefined();
      expect(step.flow.stage).toBe("confirm");
    }
    const yes = onAnswer(asked.flow, "yes");
    expect(yes.call).toEqual({ tool: "preview_tag_changes", args: { tags: { owner: "mukesh" }, service: "lambda",
      name_match: "contains", name_text: "codebridge", region: "ap-south-1" } });
  });

  it("no writes nothing and asks for a replacement; a new key or value invalidates the earlier confirmation", () => {
    const asked = onAnswer(searched(), "Key is owner, value is mukesh.");
    const no = onAnswer(asked.flow, "no");
    expect(no.call).toBeUndefined();
    expect(no.flow.stage).toBe("need_tags");
    expect(no.say).toContain("Nothing was written");
    const changed = onAnswer(asked.flow, "No, key is dev, value is mukesh.");
    expect(changed.call).toBeUndefined();
    expect(changed.flow).toMatchObject({ stage: "confirm", tag: { key: "dev", value: "mukesh" } });
    expect(changed.flow.version).toBeGreaterThan(asked.flow.version);
    // A yes (button) bound to the earlier summary does nothing.
    expect(onAnswer(changed.flow, "yes", asked.flow.version).call).toBeUndefined();
    expect(onAnswer(changed.flow, "yes").call?.args).toMatchObject({ tags: { dev: "mukesh" } });
  });

  it("the preview hands the server's request to the CLI workflow and never approves", () => {
    const yes = onAnswer(onAnswer(searched(), "Key is owner, value is mukesh.").flow, "yes");
    const ready = onPreviewResult(yes.flow, READY);
    expect(ready.flow.stage).toBe("ready");
    expect(ready.flow.preview?.request).toEqual(REQUEST);
    expect(ready.say).toContain("Nothing has been applied");
    expect(ready.say).toContain("a different person approves");
    const conflict = onPreviewResult(yes.flow, { status: "NO_CHANGES", matched: 3, changing: 0, conflicts: 3 });
    expect(conflict.flow.stage).toBe("need_tags");
  });

  it("refuses reserved keys", () => {
    const step = onAnswer(searched(), "key is aws:owner, value is x");
    expect(step.flow.stage).toBe("need_tags");
    expect(step.say).toContain("reserved");
  });
});

describe("voice", () => {
  function controller() {
    const call = vi.fn<NonNullable<VoiceDeps["call"]>>(async (tool) => ({
      structured: tool === "search_resources" ? SEARCH : READY }));
    const execute = vi.fn<VoiceDeps["execute"]>(async () => ({ speech: "x" }));
    const c = new VoiceController({ createRecognition: null, synth: null, createUtterance: null, tools: () => TOOLS,
                                    needsConfirmation: () => true, execute, call, selectedRegion: () => "ap-south-1" },
                                  undefined, { autoListen: false });
    return { c, call, execute };
  }
  const tick = () => new Promise((r) => setTimeout(r, 0));

  it("runs the whole conversation with read-only calls only", async () => {
    const { c, call, execute } = controller();
    await c.handleTranscript(ASK);
    await c.handleTranscript("yes"); // "I heard: ... Is that right?"
    expect(call).not.toHaveBeenCalled(); // the request named no region: "Which AWS region should I search ...?"
    expect(c.snapshot.message).toMatch(/^Which AWS region should I search/);
    await c.handleTranscript("Mumbai");
    await tick();
    expect(call).toHaveBeenCalledTimes(1);
    expect(c.snapshot.message).toContain("I found 3 Lambda functions");
    await c.handleTranscript("Key is owner, value is mukesh.");
    expect(c.snapshot.message).toContain("Tag value: mukesh");
    await c.handleTranscript("sounds good");
    await c.handleTranscript("No, key is dev, value is mukesh.");
    expect(call).toHaveBeenCalledTimes(1);
    expect(c.snapshot.message).toContain("Tag key: dev");
    await c.handleTranscript("yes");
    await tick();
    expect(call).toHaveBeenCalledTimes(2);
    expect(call.mock.calls[1]).toEqual(["preview_tag_changes", expect.objectContaining({ tags: { dev: "mukesh" } })]);
    expect(c.snapshot.tagFlow?.stage).toBe("ready");
    expect(c.snapshot.message).toContain("can't approve or apply tags");
    // Saying yes again, or "approve it", calls nothing more: approval and execution are not reachable from here.
    for (const said of ["yes", "approve it", "execute it", "go ahead"]) {
      await c.handleTranscript(said);
      await tick();
    }
    expect(call.mock.calls.map(([t]) => t)).toEqual(["search_resources", "preview_tag_changes"]);
    expect(execute).not.toHaveBeenCalled();
  });

  it("a stale Yes button does nothing after the tag changed", async () => {
    const { c, call } = controller();
    await c.handleTranscript(ASK);
    await c.handleTranscript("yes"); // "I heard: ... Is that right?"
    expect(call).not.toHaveBeenCalled(); // the request named no region: "Which AWS region should I search ...?"
    expect(c.snapshot.message).toMatch(/^Which AWS region should I search/);
    await c.handleTranscript("Mumbai");
    await tick();
    await c.handleTranscript("Key is owner, value is mukesh.");
    const old = c.snapshot.tagFlow!.version;
    await c.handleTranscript("key is team, value is platform");
    c.confirmTagFlow(old);
    await tick();
    expect(call).toHaveBeenCalledTimes(1);
    c.confirmTagFlow(c.snapshot.tagFlow!.version);
    await tick();
    expect(call).toHaveBeenCalledTimes(2);
  });
});
