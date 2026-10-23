// Mandatory region clarification for resource listing: a listing request that names no region asks "Which AWS
// region should I search?" before any discovery call, keeps the original request across turns, and runs it only in
// the region the user gives. Fake tools: no browser, no network, no AWS.
import type { Tool } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, it, vi } from "vitest";

import { discoveryFor, regionAnswer, regionOnly, searchingText, withRegion } from "../src/lib/discovery";
import { answerRegion, mapIntent } from "../src/lib/intent";
import { REGION_OPTIONS, regionChoices, regionQuestion, resolveRegionAnswer, SUGGESTED_REGIONS } from "../src/lib/regions";
import { READ_ONLY_TOOLS, regionSupport } from "../src/lib/tools";
import { VoiceController, type ExecuteResult, type VoiceDeps } from "../src/lib/voice";

const TOOLS = [...READ_ONLY_TOOLS, "preview_tag_changes"];
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0)); };

function setup(opts: { allRegions?: boolean; execute?: VoiceDeps["execute"]; selected?: string } = {}) {
  const execute = vi.fn<VoiceDeps["execute"]>(opts.execute ?? (async (tool, args) => (
    { speech: `${tool} answered for ${String(args.region ?? "every region")}.` })));
  const call = vi.fn<NonNullable<VoiceDeps["call"]>>(async () => ({
    structured: { mode: "fixture", total: 2, coverage_complete: true,
                  resources: [{ display_name: "codebridge-api" }, { display_name: "codebridge-cron" }] } }));
  const c = new VoiceController({ createRecognition: null, synth: null, createUtterance: null, tools: () => TOOLS,
    needsConfirmation: (t) => !READ_ONLY_TOOLS.has(t), execute, call,
    selectedRegion: () => opts.selected ?? "eu-west-1",
    regionSupport: () => ({ allRegions: opts.allRegions ?? true }) }, undefined, { autoListen: false });
  return { c, execute, call };
}

describe("region choices come from the region configuration", () => {
  it("offers the suggested regions, each a configured region, with all regions when supported", () => {
    for (const code of SUGGESTED_REGIONS) expect(REGION_OPTIONS.some((r) => r.code === code)).toBe(true);
    expect(regionChoices().map((r) => r.code)).toEqual([...SUGGESTED_REGIONS]);
    expect(regionChoices("eu-west-2")[0]).toEqual({ code: "eu-west-2", label: "Europe (London)" }); // preferred first
    expect(regionChoices("xx-nowhere-9").map((r) => r.code)).toEqual([...SUGGESTED_REGIONS]);
    const q = regionQuestion("Lambda functions");
    for (const label of ["US East (N. Virginia)", "US West (Oregon)", "Asia Pacific (Mumbai)", "Asia Pacific (Singapore)",
                         "Europe (Ireland)"]) expect(q).toContain(label);
    expect(q).toMatch(/^Which AWS region should I search for Lambda functions\?/);
    expect(q).toContain("all regions");
    expect(regionQuestion(undefined, { allRegions: false })).not.toContain("all regions");
  });

  it.each([["Mumbai", "ap-south-1"], ["Singapore", "ap-southeast-1"], ["US East.", "us-east-1"], ["US West", "us-west-2"],
           ["ap-southeast-1", "ap-southeast-1"], ["Asia Pacific (Mumbai)", "ap-south-1"], ["Ireland", "eu-west-1"],
           ["N. Virginia", "us-east-1"], ["Oregon", "us-west-2"]])("the answer %s is %s", (text, code) => {
    expect(resolveRegionAnswer(text)).toMatchObject({ kind: "region", code });
  });

  it("all-region support comes from the tool's own input schema", () => {
    const tool = (props: Record<string, unknown> | undefined) => ({ name: "t", inputSchema: { type: "object", properties: props } }) as Tool;
    expect(regionSupport(tool({ region: { pattern: "^(all|[a-z]{2}(-[a-z]+)+-\\d{1,2})$" } }))).toEqual({ allRegions: true });
    expect(regionSupport(tool({ region: { pattern: "^[a-z]{2}(-[a-z]+)+-\\d{1,2}$" } }))).toEqual({ allRegions: false });
    expect(regionSupport(tool({}))).toEqual({ allRegions: true }); // no region argument: always every enabled region
  });
});

describe("pure steps", () => {
  it("a listing without a region is a question, never a call", () => {
    for (const q of ["List all the Lambda functions.", "Find untagged resources.", "List all resources.",
                     "Find Lambda functions containing CodeBridge"]) {
      const plan = mapIntent(q, TOOLS);
      expect(plan).toMatchObject({ kind: "needs", missing: "region" });
      expect(plan.kind === "needs" && "region" in plan.args).toBe(false);
    }
  });

  it("an explicit region in the request runs at once", () => {
    expect(mapIntent("Show me all DynamoDB tables in Singapore.", TOOLS)).toMatchObject(
      { kind: "call", tool: "get_resource_inventory", args: { services: ["dynamodb"], region: "ap-southeast-1" } });
    expect(mapIntent("Find untagged resources in ap-south-1", TOOLS)).toMatchObject(
      { kind: "call", tool: "list_untagged_resources", args: { region: "ap-south-1" } });
  });

  it("all regions follows the tool's capability", () => {
    expect(regionAnswer("all regions", { allRegions: true })).toEqual({ kind: "region", region: "all" });
    expect(regionAnswer("all regions", { allRegions: false })).toMatchObject({ kind: "retry" });
    const pending = { tool: "list_untagged_resources", args: {}, reason: "r" };
    expect(answerRegion(pending, "all regions")).toMatchObject({ kind: "call", args: { region: "all" } });
    expect(answerRegion(pending, "all regions", { allRegions: false })).toMatchObject({ kind: "needs", missing: "region" });
  });

  it("withRegion replaces the region and never keeps the old one", () => {
    expect(withRegion("get_resource_inventory", { services: ["lambda"], region: "ap-south-1" }, "ap-southeast-1"))
      .toEqual({ services: ["lambda"], region: "ap-southeast-1" });
    expect(withRegion("search_resources", { text: "x", region: "ap-south-1" }, "all")).toEqual({ text: "x" });
  });

  it("regionOnly recognises a bare region reply, not a new request", () => {
    expect(regionOnly("Mumbai")).toBe("ap-south-1");
    expect(regionOnly("What about Singapore?")).toBe("ap-southeast-1");
    expect(regionOnly("Try us-east-1 instead")).toBe("us-east-1");
    expect(regionOnly("All regions")).toBe("all");
    for (const q of ["Show me DynamoDB tables in Singapore", "Show my resources in Mumbai", "What was my bill?", "no", "hello"]) {
      expect(regionOnly(q)).toBeUndefined();
    }
  });

  it("the read-back says what and where", () => {
    expect(searchingText("get_resource_inventory", { services: ["lambda"], region: "ap-south-1" }))
      .toBe("Searching Lambda functions in Asia Pacific (Mumbai).");
    expect(searchingText("list_untagged_resources", { region: "us-east-1" })).toBe("Searching untagged resources in US East (N. Virginia).");
    expect(searchingText("get_resource_inventory", { region: "all" })).toBe("Searching your AWS resources in all supported regions.");
    expect(discoveryFor(1, "List Lambdas", "get_resource_inventory", { services: ["lambda"] }, "COLLECT_REGION"))
      .toEqual({ id: 1, stage: "COLLECT_REGION", request: "List Lambdas", tool: "get_resource_inventory",
                 args: { services: ["lambda"] }, services: ["lambda"] });
  });
});

describe("voice conversation", () => {
  it("Example 1: List all Lambda functions -> which region? -> Mumbai searches only ap-south-1", async () => {
    const { c, execute } = setup();
    await c.handleTranscript("List all Lambda functions.");
    await flush();
    expect(execute).not.toHaveBeenCalled(); // no discovery before the region is known
    expect(c.snapshot.message).toMatch(/^Which AWS region should I search for Lambda functions\?/);
    expect(c.snapshot.discovery).toMatchObject({ stage: "COLLECT_REGION", request: "List all Lambda functions.",
                                                 services: ["lambda"] });
    await c.handleTranscript("Mumbai.");
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith("get_resource_inventory", { services: ["lambda"], region: "ap-south-1" });
    expect(c.snapshot.message).toMatch(/^Searching Lambda functions in Asia Pacific \(Mumbai\)\./);
    expect(c.snapshot.discovery).toMatchObject({ stage: "DISPLAY_RESULTS", region: "ap-south-1" });
  });

  it("never uses the selected default region in place of asking", async () => {
    const { c, execute } = setup({ selected: "ap-south-1" });
    await c.handleTranscript("List all Lambda functions.");
    await flush();
    expect(execute).not.toHaveBeenCalled();
  });

  it("Example 2: an explicit region (Singapore) searches at once without asking", async () => {
    const { c, execute } = setup();
    await c.handleTranscript("Show me all DynamoDB tables in Singapore.");
    await flush();
    expect(execute).toHaveBeenCalledWith("get_resource_inventory", { services: ["dynamodb"], region: "ap-southeast-1" });
    expect(c.snapshot.clarifying).toBeNull();
  });

  it("Example 3: Find untagged resources -> US East searches us-east-1", async () => {
    const { c, execute } = setup();
    await c.handleTranscript("Find untagged resources.");
    await flush();
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.message).toMatch(/^Which AWS region should I search/);
    await c.handleTranscript("US East.");
    await flush();
    expect(execute).toHaveBeenCalledWith("list_untagged_resources", { region: "us-east-1" });
    expect(c.snapshot.message).toMatch(/^Searching untagged resources in US East \(N\. Virginia\)\./);
  });

  it("Example 4: List all resources -> All regions uses multi-region discovery when the tool supports it", async () => {
    const { c, execute } = setup({ allRegions: true });
    await c.handleTranscript("List all resources.");
    await c.handleTranscript("All regions.");
    await flush();
    expect(execute).toHaveBeenCalledWith("get_resource_inventory", { region: "all" });
  });

  it("all regions is refused, with nothing run, when the tool can't search every region", async () => {
    const { c, execute } = setup({ allRegions: false });
    await c.handleTranscript("List all resources.");
    await c.handleTranscript("All regions.");
    await flush();
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.message).toMatch(/can't search every region/);
    expect(c.snapshot.clarifying).toMatchObject({ missing: "region", tool: "get_resource_inventory" });
    await c.handleTranscript("Singapore");
    await flush();
    expect(execute).toHaveBeenCalledWith("get_resource_inventory", { region: "ap-southeast-1" });
  });

  it("an invalid or ambiguous region asks again; the original request survives several turns", async () => {
    const { c, execute } = setup();
    await c.handleTranscript("List all Lambda functions.");
    await c.handleTranscript("Atlantis");
    expect(c.snapshot.message).toMatch(/didn't recognise that region/);
    await c.handleTranscript("India");
    expect(c.snapshot.message).toMatch(/Asia Pacific \(Mumbai\) or Asia Pacific \(Hyderabad\)/);
    await flush();
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.clarifying).toMatchObject({ request: "List all Lambda functions.", args: { services: ["lambda"] } });
    await c.handleTranscript("Mumbai");
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith("get_resource_inventory", { services: ["lambda"], region: "ap-south-1" });
  });

  it("a complete new question replaces the open region question", async () => {
    const { c, execute } = setup();
    await c.handleTranscript("List all Lambda functions.");
    await c.handleTranscript("What was my AWS bill last month?");
    await flush();
    expect(execute).toHaveBeenCalledWith("get_monthly_aws_bill", { month: "last-month" });
    expect(c.snapshot.clarifying).toBeNull();
  });

  it("changing the region after results runs a new search in that region only", async () => {
    const { c, execute } = setup();
    await c.handleTranscript("List all Lambda functions in Mumbai.");
    await flush();
    const first = c.snapshot.discovery!;
    await c.handleTranscript("What about Singapore?");
    await flush();
    expect(execute).toHaveBeenLastCalledWith("get_resource_inventory", { services: ["lambda"], region: "ap-southeast-1" });
    expect(c.snapshot.discovery!.id).not.toBe(first.id); // a new context, not the old one
    expect(c.snapshot.message).toMatch(/^Searching Lambda functions in Asia Pacific \(Singapore\)\. get_resource_inventory answered for ap-southeast-1/);
  });

  it("changing the region while a search runs drops the old region's answer", async () => {
    const waiting: ((r: ExecuteResult) => void)[] = [];
    const { c, execute } = setup({ execute: () => new Promise((resolve) => { waiting.push(resolve); }) });
    await c.handleTranscript("List all Lambda functions.");
    await c.handleTranscript("Mumbai");
    expect(execute).toHaveBeenCalledTimes(1);
    expect(c.snapshot.discovery?.stage).toBe("DISCOVER_RESOURCES");
    await c.handleTranscript("Actually, Singapore.");
    expect(execute).toHaveBeenCalledTimes(2);
    expect(execute.mock.calls[1]).toEqual(["get_resource_inventory", { services: ["lambda"], region: "ap-southeast-1" }]);
    waiting[1]({ speech: "2 Lambda functions in Singapore." });
    await flush();
    waiting[0]({ speech: "5 Lambda functions in Mumbai." }); // the stale answer arrives last
    await flush();
    expect(c.snapshot.message).toContain("in Singapore");
    expect(c.snapshot.message).not.toContain("Mumbai");
  });

  it("cancel drops the pending request; nothing runs", async () => {
    const { c, execute } = setup();
    await c.handleTranscript("List all Lambda functions.");
    await c.handleTranscript("never mind");
    await c.handleTranscript("Mumbai");
    await flush();
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.discovery).toBeNull();
  });

  it("Example 5: bulk tagging reads back, confirms, asks the region, then searches (preview-only, no write)", async () => {
    const { c, execute, call } = setup();
    await c.handleTranscript("Find all Lambda functions with CodeBridge and tag them.");
    expect(c.snapshot.heardCheck).not.toBeNull();
    expect(c.snapshot.discovery?.stage).toBe("CONFIRM_INTERPRETATION");
    expect(call).not.toHaveBeenCalled();
    await c.handleTranscript("yes");
    expect(c.snapshot.discovery?.stage).toBe("COLLECT_REGION");
    expect(call).not.toHaveBeenCalled();
    await c.handleTranscript("Mumbai");
    await flush();
    expect(call).toHaveBeenCalledTimes(1);
    expect(call).toHaveBeenCalledWith("search_resources", { text: "CodeBridge", match: "contains", services: ["lambda"],
                                                            region: "ap-south-1", limit: 100 });
    expect(c.snapshot.tagFlow).toMatchObject({ region: "ap-south-1", stage: "need_tags" });
    expect(execute).not.toHaveBeenCalled(); // nothing else ran; the existing tag -> preview flow continues from here
  });

  it("a region named in the tagging request is used without asking again", async () => {
    const { c, call } = setup();
    await c.handleTranscript("Find all Lambda functions with CodeBridge in Singapore and tag them.");
    expect(c.snapshot.heardCheck?.region).toBe("ap-southeast-1");
    await c.handleTranscript("yes");
    await flush();
    expect(call).toHaveBeenCalledWith("search_resources", expect.objectContaining({ region: "ap-southeast-1" }));
  });

  it("interim words can't answer the region question", async () => {
    const { c, execute } = setup();
    await c.handleTranscript("List all Lambda functions.");
    await c.handleTranscript("Mumbai", { final: false });
    await flush();
    expect(execute).not.toHaveBeenCalled();
  });
});
