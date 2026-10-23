// Region-aware inventory: region names to codes, the "all regions or a specific region?" clarification (typed and
// spoken), and service-specific scope. Fake speech APIs and a spy for `execute`: no browser, no network, no AWS.
import { describe, expect, it, vi } from "vitest";

import { answerRegion, mapIntent } from "../src/lib/intent";
import { READ_ONLY_TOOLS } from "../src/lib/tools";
import { REGION_QUESTION, resolveRegion } from "../src/lib/regions";
import { VoiceController, type VoiceDeps } from "../src/lib/voice";

const ALL = [...READ_ONLY_TOOLS];

describe("resolveRegion", () => {
  it.each([
    ["Mumbai", "ap-south-1"], ["mumbai.", "ap-south-1"], ["Singapore", "ap-southeast-1"],
    ["US East (N. Virginia)", "us-east-1"], ["N. Virginia", "us-east-1"], ["Ireland", "eu-west-1"],
    ["ap-south-1", "ap-south-1"], ["in eu-central-1 please", "eu-central-1"], ["Tokyo", "ap-northeast-1"],
  ])("%s -> %s", (text, code) => {
    expect(resolveRegion(text)).toMatchObject({ kind: "region", code });
  });

  it.each(["All regions", "all AWS regions", "every region", "check everywhere", "all the regions"])(
    "%s -> all", (text) => {
      expect(resolveRegion(text)).toEqual({ kind: "all" });
    });

  it.each([["US East", ["us-east-1", "us-east-2"]], ["India", ["ap-south-1", "ap-south-2"]],
           ["Mumbai and Singapore", ["ap-south-1", "ap-southeast-1"]]])(
    "%s is ambiguous, never guessed", (text, candidates) => {
      expect(resolveRegion(text)).toEqual({ kind: "ambiguous", candidates });
    });

  it.each(["Atlantis", "somewhere", "", "show us the money"])("%s names no region", (text) => {
    expect(resolveRegion(text)).toEqual({ kind: "none" });
  });
});

describe("inventory intent", () => {
  it.each(["Show me my AWS resources.", "What resources do I have?", "Give me my infrastructure overview."])(
    "a broad question asks which region first (no default, no silent all-region scan): %s", (q) => {
      const plan = mapIntent(q, ALL);
      expect(plan).toEqual(expect.objectContaining({ kind: "needs", tool: "get_resource_inventory", missing: "region",
                                                     args: {} }));
      expect(plan.kind === "needs" && plan.prompt).toBe(REGION_QUESTION);
    });

  it("a named region that isn't known asks, never falling back to all regions", () => {
    const plan = mapIntent("Show my resources in Atlantis", ALL);
    expect(plan).toMatchObject({ kind: "needs", missing: "region" });
    expect(plan.kind === "needs" && plan.prompt).toContain(REGION_QUESTION);
  });

  it("an explicit region does not ask again", () => {
    expect(mapIntent("Show me my AWS resources in Mumbai.", ALL))
      .toMatchObject({ kind: "call", tool: "get_resource_inventory", args: { region: "ap-south-1" } });
  });

  it("all regions keeps the complete regional discovery", () => {
    expect(mapIntent("Show me my AWS resources in all regions", ALL))
      .toMatchObject({ kind: "call", args: { region: "all" } });
  });

  it.each([["Show my Lambda functions in Mumbai", ["lambda"]], ["List my DynamoDB tables in Ireland", ["dynamodb"]],
           ["Show my S3 buckets in all regions", ["s3"]], ["List my EC2 instances in ap-south-1", ["ec2"]]])(
    "%s queries only that service", (q, services) => {
      const plan = mapIntent(q, ALL);
      expect(plan.kind === "call" && plan.args.services).toEqual(services);
    });

  it("a service-specific question without a region asks which region, naming the type", () => {
    const plan = mapIntent("Show my Lambda functions", ALL);
    expect(plan).toMatchObject({ kind: "needs", missing: "region", args: { services: ["lambda"] } });
    expect(plan.kind === "needs" && plan.prompt).toMatch(/^Which AWS region should I search for Lambda functions\?/);
  });

  it("the answer to the region question completes the pending call, not a new question", () => {
    const pending = { args: { services: ["lambda"] }, reason: "r" };
    expect(answerRegion(pending, "Mumbai")).toMatchObject(
      { kind: "call", tool: "get_resource_inventory", args: { services: ["lambda"], region: "ap-south-1" } });
    expect(answerRegion(pending, "all regions")).toMatchObject({ kind: "call", args: { region: "all" } });
  });

  it("an unsupported or ambiguous answer asks again", () => {
    const pending = { args: {}, reason: "r" };
    expect(answerRegion(pending, "Atlantis")).toMatchObject({ kind: "needs", missing: "region" });
    const again = answerRegion(pending, "India");
    expect(again.kind === "needs" && again.prompt).toMatch(/Asia Pacific \(Mumbai\) or Asia Pacific \(Hyderabad\)/);
  });

  it("an answer naming an offered choice picks it: US East is the offered US East (N. Virginia)", () => {
    const pending = { args: {}, reason: "r" };
    expect(answerRegion(pending, "US East.")).toMatchObject({ kind: "call", args: { region: "us-east-1" } });
    expect(answerRegion(pending, "US West")).toMatchObject({ kind: "call", args: { region: "us-west-2" } });
    expect(answerRegion(pending, "all regions", { allRegions: false })).toMatchObject({ kind: "needs", missing: "region" });
  });
});

describe("spoken clarification", () => {
  function setup() {
    const execute = vi.fn<VoiceDeps["execute"]>(async () => ({ speech: "Done." }));
    const c = new VoiceController({ createRecognition: null, synth: null, createUtterance: null, tools: () => ALL,
                                    needsConfirmation: () => false, execute }, undefined, { autoListen: false });
    return { c, execute };
  }
  const flush = () => new Promise((r) => setTimeout(r, 0));

  it("an unknown region asks, then runs ap-south-1 when the answer is Mumbai", async () => {
    const { c, execute } = setup();
    await c.handleTranscript("Show me my AWS resources in Atlantis.");
    expect(execute).not.toHaveBeenCalled(); // never an all-region scan for a region that wasn't understood
    expect(c.snapshot.message).toContain(REGION_QUESTION);
    expect(c.snapshot.clarifying).not.toBeNull();
    await c.handleTranscript("Mumbai.");
    await flush();
    expect(execute).toHaveBeenCalledWith("get_resource_inventory", { region: "ap-south-1" });
    expect(c.snapshot.clarifying).toBeNull();
  });

  it("an explicit region runs at once", async () => {
    const { c, execute } = setup();
    await c.handleTranscript("Show me my AWS resources in Mumbai.");
    await flush();
    expect(execute).toHaveBeenCalledWith("get_resource_inventory", { region: "ap-south-1" });
  });

  it("an unrecognised region asks again and runs nothing", async () => {
    const { c, execute } = setup();
    await c.handleTranscript("What resources do I have in Atlantis?");
    await c.handleTranscript("Atlantis");
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.clarifying).not.toBeNull();
    expect(c.snapshot.message).toMatch(/didn't recognise that region/);
    await c.handleTranscript("all regions");
    await flush();
    expect(execute).toHaveBeenCalledWith("get_resource_inventory", { region: "all" });
  });

  it("cancel drops the question; a different question replaces it", async () => {
    const { c, execute } = setup();
    await c.handleTranscript("Show me my AWS resources in Atlantis.");
    await c.handleTranscript("cancel");
    expect(c.snapshot.clarifying).toBeNull();
    expect(execute).not.toHaveBeenCalled();
    await c.handleTranscript("Show me my AWS resources in Atlantis.");
    await c.handleTranscript("Which AWS account am I using?");
    await flush();
    expect(execute).toHaveBeenCalledWith("get_aws_account_identity", {});
    expect(c.snapshot.clarifying).toBeNull();
  });
});
