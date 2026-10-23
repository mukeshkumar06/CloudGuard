// Name search ("Lambdas containing ecommerce") and tag-change requests in the simulator. The simulator's Alexa token
// can never write, and nothing said to it applies tags: tagging requests and approval phrases are refused with an
// explanation and run no tool. Fake speech APIs and a spy for the tools: no browser, no network, no AWS.
import { describe, expect, it, vi } from "vitest";

import { TAG_CHANGE_REFUSAL, answerRegion, mapIntent, nameFilterIn } from "../src/lib/intent";
import { spokenAnswer } from "../src/lib/resources";
import { READ_ONLY_TOOLS } from "../src/lib/tools";
import { VoiceController, type VoiceDeps } from "../src/lib/voice";

const ALL = [...READ_ONLY_TOOLS];
const plan = (q: string) => mapIntent(q, ALL);

describe("name search", () => {
  it.each([
    ["List Lambda functions containing ecommerce.", { text: "ecommerce", match: "contains", services: ["lambda"] }],
    ["Find Lambdas starting with A.", { text: "A", match: "starts_with", services: ["lambda"] }],
    ["Show Lambda functions beginning with C.", { text: "C", match: "starts_with", services: ["lambda"] }],
    ["Find resources containing payment.", { text: "payment", match: "contains" }],
    ["List DynamoDB tables starting with user.", { text: "user", match: "starts_with", services: ["dynamodb"] }],
    ["Find S3 buckets containing assets.", { text: "assets", match: "contains", services: ["s3"] }],
    ["Show EC2 instances containing production.", { text: "production", match: "contains", services: ["ec2"] }],
    ["Show all resources starting with cloudguard.", { text: "cloudguard", match: "starts_with" }],
    ["Find buckets ending with logs", { text: "logs", match: "ends_with", services: ["s3"] }],
    ["Find the Lambda named exactly OrderProcessor", { text: "OrderProcessor", match: "exact", services: ["lambda"] }],
  ])("%s (no region: asks which one first)", (q, args) => {
    expect(plan(q)).toEqual(expect.objectContaining({ kind: "needs", tool: "search_resources", missing: "region", args }));
  });

  it("a named region runs the search at once", () => {
    expect(plan("Find DynamoDB tables starting with user in Mumbai.")).toEqual(expect.objectContaining({
      kind: "call", tool: "search_resources",
      args: { text: "user", match: "starts_with", services: ["dynamodb"], region: "ap-south-1" } }));
    const all = plan("Find Lambdas containing ecommerce in all regions");
    expect(all.kind === "call" && "region" in all.args).toBe(false); // every region: search_resources' own default
  });

  it("an unknown region is asked about, and the answer keeps the search", () => {
    const p = plan("Find resources starting with A in Atlantis");
    expect(p).toMatchObject({ kind: "needs", tool: "search_resources", missing: "region" });
    const pending = { tool: "search_resources", args: { text: "A", match: "starts_with" }, reason: "r" };
    expect(answerRegion(pending, "Mumbai")).toMatchObject(
      { kind: "call", tool: "search_resources", args: { text: "A", match: "starts_with", region: "ap-south-1" } });
    expect(answerRegion(pending, "all")).toEqual(expect.objectContaining(
      { kind: "call", tool: "search_resources", args: { text: "A", match: "starts_with" } }));
  });

  it.each(["Find resources tagged with Environment", "Find Lambdas with tag Owner", "List my untagged Lambdas"])(
    "a tag filter is never a name filter: %s", (q) => {
      expect(nameFilterIn(q)).toBeUndefined();
      expect(plan(q)).not.toMatchObject({ tool: "search_resources" });
    });

  it("speaks the names the search returned, never invented ones", () => {
    expect(spokenAnswer("search_resources", {}, {
      speech: "2 Lambda functions have a name that contains that text, in 1 region.",
      resources: [{ finding_id: "tagcomp-1", display_name: "ecommerce-api", service: "lambda" },
                  { finding_id: "tagcomp-2", display_name: "ecommerce-worker", service: "lambda" }],
    }).speech).toBe("2 Lambda functions have a name that contains that text, in 1 region. They are: ecommerce-api "
      + "and ecommerce-worker.");
  });
});

describe("tag changes are never made from the simulator", () => {
  it.each([
    "Add Environment as Production and Owner as Backend to all three.",
    "Add these tags to all ecommerce Lambdas.",
    "Only update the production ones.",
    "Show me the changes first.",
    "Yes, apply these tags.",
    "Apply the approved changes.",
    "Approve the proposal",
    "Tag all my Lambdas with Owner=me",
  ])("%s -> explained, no tool", (q) => {
    expect(plan(q)).toEqual({ kind: "none", reason: TAG_CHANGE_REFUSAL });
  });

  it("'What tags should I add?' still recommends (read-only), it doesn't apply", () => {
    expect(plan("What tags should I add?")).toMatchObject({ tool: "recommend_tags" });
  });

  it("by voice, approval phrases run nothing, even right after a recommendation", async () => {
    const execute = vi.fn<VoiceDeps["execute"]>(async () => ({ speech: "For OrderProcessor: ... Nothing was changed.",
      resource: { finding_id: "tagcomp-0123456789abcdef", name: "OrderProcessor", service: "lambda" } }));
    const c = new VoiceController({ createRecognition: null, synth: null, createUtterance: null, tools: () => ALL,
                                    needsConfirmation: () => false, execute }, undefined, { autoListen: false });
    await c.handleTranscript("What tags should I add to OrderProcessor?");
    await new Promise((r) => setTimeout(r, 0));
    expect(execute).toHaveBeenCalledTimes(1);
    for (const said of ["Yes, apply these tags.", "Sounds good", "Apply the approved changes.", "yes", "go ahead"]) {
      await c.handleTranscript(said);
      await new Promise((r) => setTimeout(r, 0));
    }
    expect(execute).toHaveBeenCalledTimes(1); // only the read-only recommendation ever ran
    expect(execute.mock.calls.every(([tool]) => READ_ONLY_TOOLS.has(tool))).toBe(true);
  });
});
