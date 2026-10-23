// Resource discovery by voice: the region is always asked for when none is named, "untagged" and its mishearing "unpacked", plain-language
// tagging questions, untagged resources by type, and resources by name ("Why is OrderProcessor untagged?") with
// follow-ups. Fake speech APIs and a spy for the tools: no browser, no network, no AWS. Every name and tag below
// is what the fake tool "returned"; nothing is invented by the code under test.
import { describe, expect, it, vi } from "vitest";

import { answerResourceName, mapIntent } from "../src/lib/intent";
import { resolveRegion } from "../src/lib/regions";
import { resourceIn, spokenAnswer } from "../src/lib/resources";
import { READ_ONLY_TOOLS } from "../src/lib/tools";
import { VoiceController, type ExecuteResult, type VoiceDeps } from "../src/lib/voice";

const ALL = [...READ_ONLY_TOOLS];
const plan = (q: string) => mapIntent(q, ALL);

describe("regions: asked for unless one is named", () => {
  it.each(["Show resources in all regions", "Check every region", "Show my resources globally"])(
    "%s -> all regions (asked for explicitly)", (q) => {
      expect(plan(q)).toMatchObject({ kind: "call", tool: "get_resource_inventory", args: { region: "all" } });
    });

  it.each(["Show my S3 buckets.", "Show my AWS resources.", "What resources do I have?"])(
    "%s -> asks which region; nothing runs and no default is assumed", (q) => {
      const p = plan(q);
      expect(p).toMatchObject({ kind: "needs", tool: "get_resource_inventory", missing: "region" });
      expect(p.kind === "needs" && "region" in p.args).toBe(false);
    });

  it.each([["Show resources in Mumbai", "ap-south-1"], ["Show Lambdas in us-east-1", "us-east-1"],
           ["Show my resources in Singapore", "ap-southeast-1"]])("%s -> %s", (q, region) => {
    expect(plan(q)).toMatchObject({ kind: "call", args: { region } });
  });

  it.each(["Show resources in Atlantis", "List my buckets in Narnia"])(
    "%s asks; an unknown region is never treated as all regions", (q) => {
      expect(plan(q)).toMatchObject({ kind: "needs", missing: "region" });
    });

  it.each(["all", "All.", "all of them", "everything", "every region", "globally", "everywhere"])(
    "the answer %s means all regions", (a) => {
      expect(resolveRegion(a)).toEqual({ kind: "all" });
    });
});

describe("untagged resources, in plain words", () => {
  it.each([
    ["List all untagged resources.", undefined], ["List all unpacked resources.", undefined],
    ["Show me unpacked resources.", undefined], ["Show me untagged resources.", undefined],
    ["Find resources without tags.", undefined], ["Which resources are missing tags?", undefined],
    ["Find resources that have no tags.", undefined], ["Which resources need tags?", undefined],
    ["Show untagged Lambdas.", ["lambda"]], ["List all the Lambdas which are untagged.", ["lambda"]],
    ["Which Lambda functions are missing tags?", ["lambda"]], ["Show DynamoDB tables without tags.", ["dynamodb"]],
    ["Show my untagged DynamoDB tables", ["dynamodb"]], ["Find untagged EC2 instances.", ["ec2"]],
    ["List S3 buckets missing tags.", ["s3"]], ["List S3 buckets without tags", ["s3"]],
  ])("%s -> list_untagged_resources %j, after asking which region", (q, services) => {
    const p = plan(q);
    expect(p).toMatchObject({ kind: "needs", tool: "list_untagged_resources", missing: "region" });
    expect(p.kind === "needs" && p.args.services).toEqual(services);
  });

  it.each(["I unpacked my bags", "unpacked", "What did I unpack last month?"])(
    "'unpacked' outside a resource question is not 'untagged': %s", (q) => {
      expect(plan(q)).not.toMatchObject({ tool: "list_untagged_resources" });
    });

  it.each(["Which resources have incorrect tags?", "Find resources that don't follow our tagging rules.",
           "Show me tagging problems.", "Check whether my AWS resources have the required tags.",
           "Check my tag compliance", "Are my resources tagged properly?"])("%s -> scan_tag_compliance", (q) => {
    expect(plan(q)).toMatchObject({ kind: "call", tool: "scan_tag_compliance" });
  });

  it("listing a type names its resources (inventory with that service, once the region is known)", () => {
    expect(plan("List all Lambdas")).toMatchObject({ kind: "needs", tool: "get_resource_inventory", missing: "region",
                                                     args: { services: ["lambda"] } });
    expect(plan("List all Lambdas in Mumbai")).toMatchObject({ kind: "call", tool: "get_resource_inventory",
                                                               args: { services: ["lambda"], region: "ap-south-1" } });
  });
});

describe("resources by name", () => {
  it.each([
    ["Why is OrderProcessor untagged?", { name: "OrderProcessor" }],
    ["Explain the tagging issue with my Payments API Lambda.", { name: "Payments API", service: "lambda" }],
    ["Why is my customer database non-compliant?", { name: "customer", service: "dynamodb" }],
    ["Explain the issue with my production EC2 instance.", { name: "production", service: "ec2" }],
    ["Why is my assets bucket missing tags?", { name: "assets", service: "s3" }],
    ["What tags should I add to OrderProcessor?", { name: "OrderProcessor" }],
    ["Why is my Lambda function non-compliant?", { service: "lambda" }],
    ["Explain this finding for my Lambda", { service: "lambda" }],
    ["Why is it untagged?", {}],
  ])("%s -> %j", (q, expected) => {
    expect(resourceIn(q)).toEqual(expected);
  });

  it("a named resource is explained by name; no ARN or finding ID is needed", () => {
    expect(plan("Why is OrderProcessor untagged?")).toEqual(expect.objectContaining({
      kind: "call", tool: "explain_finding", args: { resource_name: "OrderProcessor" } }));
    expect(plan("Explain the tagging issue with my Payments API Lambda.")).toEqual(expect.objectContaining({
      kind: "call", args: { resource_name: "Payments API", service: "lambda" } }));
  });

  it("no name asks which one, by type, in plain words", () => {
    const p = plan("Why is my Lambda function non-compliant?");
    expect(p).toMatchObject({ kind: "needs", missing: "resource_name", args: { service: "lambda" },
                              prompt: "Which Lambda function would you like me to explain?" });
    expect(p.kind === "needs" && p.prompt).not.toMatch(/ARN|tagcomp/);
  });

  it("the answer to 'which one?' is the name, with the type already asked about", () => {
    const pending = { tool: "explain_finding", args: { service: "lambda" }, reason: "r" };
    expect(answerResourceName(pending, "OrderProcessor")).toMatchObject(
      { kind: "call", args: { service: "lambda", resource_name: "OrderProcessor" } });
    expect(answerResourceName(pending, "it")).toMatchObject({ kind: "needs", missing: "resource_name" });
  });

  it("a finding ID still works", () => {
    expect(plan("Explain finding tagcomp-0123456789abcdef")).toMatchObject(
      { kind: "call", args: { finding_id: "tagcomp-0123456789abcdef" } });
  });
});

describe("spoken answers use only the tool's own result", () => {
  it("lists safe names, never withheld ones", () => {
    const said = spokenAnswer("list_untagged_resources", { services: ["lambda"] }, {
      speech: "3 Lambda functions are missing mandatory tags. Showing 1 to 3.",
      resources: [{ finding_id: "tagcomp-1", display_name: "OrderProcessor", service: "lambda" },
                  { finding_id: "tagcomp-2", display_name: "nightly-etl", service: "lambda" },
                  { finding_id: "tagcomp-3", display_name: null, service: "lambda" }],
    });
    expect(said.speech).toBe("3 Lambda functions are missing mandatory tags. Showing 1 to 3. They are: OrderProcessor "
      + "and nightly-etl. 1 more has a name I won't read out.");
  });

  it("explains a finding by name from its missing and invalid tags", () => {
    const said = spokenAnswer("explain_finding", { resource_name: "OrderProcessor" }, {
      speech: "The lambda resource in ap-south-1 (finding 2 of 8) has 2 tag problems: missing Owner, Environment.",
      finding_id: "tagcomp-1", display_name: "OrderProcessor", service: "lambda", compliance_status: "NON_COMPLIANT",
      missing_tags: ["Owner", "Environment"], invalid_tags: [],
    });
    expect(said.speech).toBe("OrderProcessor is missing the required Owner and Environment tags.");
    expect(said.resource).toEqual({ finding_id: "tagcomp-1", name: "OrderProcessor", service: "lambda" });
  });

  it("keeps the server's speech when the name was withheld", () => {
    const speech = "The lambda resource in ap-south-1 (finding 2 of 8) has 1 tag problem: missing Owner.";
    expect(spokenAnswer("explain_finding", {}, { speech, finding_id: "tagcomp-1", display_name: null,
                                                 compliance_status: "NON_COMPLIANT", missing_tags: ["Owner"] }).speech)
      .toBe(speech);
  });

  it("names the resource in a recommendation", () => {
    expect(spokenAnswer("recommend_tags", {}, {
      speech: "For the lambda resource in ap-south-1 (finding 2 of 8): Owner needs a person to provide the value. Nothing was changed.",
      finding_id: "tagcomp-1", display_name: "OrderProcessor",
    }).speech).toBe("For OrderProcessor: Owner needs a person to provide the value. Nothing was changed.");
  });
});

describe("a voice conversation about untagged Lambdas", () => {
  function setup(results: Record<string, ExecuteResult>, names: string[] = []) {
    const execute = vi.fn<VoiceDeps["execute"]>(async (tool) => results[tool] ?? { speech: "Done." });
    const candidateNames = vi.fn(async () => names);
    const c = new VoiceController({ createRecognition: null, synth: null, createUtterance: null, tools: () => ALL,
                                    needsConfirmation: () => false, execute, candidateNames },
                                  undefined, { autoListen: false });
    return { c, execute, candidateNames };
  }
  const flush = () => new Promise((r) => setTimeout(r, 0));
  const ORDER = { finding_id: "tagcomp-0123456789abcdef", name: "OrderProcessor", service: "lambda" };

  it("list, explain by name, then 'What tags should I add?' about the same resource", async () => {
    const { c, execute } = setup({
      list_untagged_resources: { speech: "3 Lambda functions are missing mandatory tags. They are: ..." },
      explain_finding: { speech: "OrderProcessor is missing the required Owner and Environment tags.", resource: ORDER },
      recommend_tags: { speech: "For OrderProcessor: ... Nothing was changed.", resource: ORDER },
    });
    await c.handleTranscript("List all the Lambdas which are untagged.");
    await flush();
    expect(execute).not.toHaveBeenCalled(); // which region? first
    await c.handleTranscript("Mumbai");
    await flush();
    expect(execute).toHaveBeenLastCalledWith("list_untagged_resources", { services: ["lambda"], region: "ap-south-1" });
    expect(c.snapshot.message).toMatch(/^Searching untagged Lambda functions in Asia Pacific \(Mumbai\)\. 3 Lambda/);
    await c.handleTranscript("Why is OrderProcessor untagged?");
    await flush();
    expect(execute).toHaveBeenLastCalledWith("explain_finding", { resource_name: "OrderProcessor" });
    expect(c.snapshot.message).toBe("OrderProcessor is missing the required Owner and Environment tags.");
    await c.handleTranscript("What tags should I add?");
    await flush();
    expect(execute).toHaveBeenLastCalledWith("recommend_tags", { resource: ORDER.finding_id });
    expect(execute).toHaveBeenCalledTimes(3); // no confirmation was needed: every call is read-only
  });

  it("'Which Lambda function?' lists names from the tool, then the spoken name is explained", async () => {
    const { c, execute, candidateNames } = setup({}, ["OrderProcessor", "nightly-etl"]);
    await c.handleTranscript("Why is my Lambda function non-compliant?");
    await flush();
    expect(candidateNames).toHaveBeenCalledWith(["lambda"]);
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.message).toBe("Which Lambda function would you like me to explain? For example: OrderProcessor "
      + "and nightly-etl.");
    await c.handleTranscript("OrderProcessor");
    await flush();
    expect(execute).toHaveBeenCalledWith("explain_finding", { service: "lambda", resource_name: "OrderProcessor" });
    expect(c.snapshot.clarifying).toBeNull();
  });

  it("cancel drops the 'which one?' question and runs nothing", async () => {
    const { c, execute } = setup({});
    await c.handleTranscript("Why is my Lambda function non-compliant?");
    await flush();
    await c.handleTranscript("never mind");
    expect(c.snapshot.clarifying).toBeNull();
    expect(execute).not.toHaveBeenCalled();
  });

  it("a server error about the name is spoken as the answer, and nothing is guessed", async () => {
    const { c, execute } = setup({ explain_finding: { isError: true,
      errorText: "2 scanned resources match that name: nightly-etl (Lambda function in ap-south-1); nightly-etl "
        + "(Lambda function in us-east-1). Say which service or region." } });
    await c.handleTranscript("Why is nightly-etl untagged?");
    await flush();
    expect(execute).toHaveBeenCalledTimes(1);
    expect(c.snapshot.message).toMatch(/2 scanned resources match that name/);
  });
});
