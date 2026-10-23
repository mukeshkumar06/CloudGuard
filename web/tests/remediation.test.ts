// Regression tests for the QA audit fixes (Phase A and B): identity and invalid-tag intents, invalid months, named
// months in comparisons, forced refresh arguments, and voice refresh. No browser, no network, no AWS.
import type { Tool } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, it, vi } from "vitest";

import { mapIntent, monthsIn } from "../src/lib/intent";
import { READ_ONLY_TOOLS, withRefresh } from "../src/lib/tools";
import { VoiceController, type VoiceDeps } from "../src/lib/voice";

const ALL = [...READ_ONLY_TOOLS];
const TODAY = new Date(2026, 9, 22); // 22 October 2026
const plan = (q: string) => mapIntent(q, ALL, TODAY);

describe("identity phrasings (audit #2, #4, #5)", () => {
  it.each(["Which AWS account am I connected to?", "Which AWS identity are you using?", "Who am I logged in as?",
           "Show my AWS account details.", "Which IAM role am I using?", "What role am I using?"])(
    "%s -> get_aws_account_identity", (q) => {
      expect(plan(q)).toMatchObject({ kind: "call", tool: "get_aws_account_identity", args: {} });
    });

  it.each([["Which account costs the most?", "get_cost_breakdown"],
           ["How much did my AWS account cost last month?", "get_monthly_aws_bill"]])(
    "%s stays a cost question", (q, tool) => {
      expect(plan(q)).toMatchObject({ tool });
    });
});

describe("invalid tag values select the compliance scan (audit #36)", () => {
  it.each(["Which resources have invalid tag values?", "Show resources with invalid tags.", "Find tag policy violations.",
           "Which resources are violating my tagging policy?"])("%s -> scan_tag_compliance", (q) => {
    expect(plan(q)).toMatchObject({ kind: "call", tool: "scan_tag_compliance", args: {} });
  });
});

describe("invalid months are never replaced by another month (audit #16)", () => {
  it.each(["What did I pay in 2026-13?", "What was my bill for 2026-00?", "Cost breakdown for 2026-14"])(
    "%s asks which month, and runs nothing", (q) => {
      const p = plan(q);
      expect(p.kind).toBe("needs");
      expect(p.kind === "needs" && p.missing).toBe("month");
      expect(p.kind === "needs" && p.prompt).toMatch(/isn't a valid month/);
    });

  it("an invalid month in a comparison is asked about too", () => {
    expect(plan("Compare 2026-13 with 2026-09")).toMatchObject({ kind: "needs", missing: "month" });
  });

  it("no month still uses the tool's default; valid months are passed as named", () => {
    expect(plan("What was my AWS bill?")).toEqual(expect.objectContaining({ kind: "call", args: {} }));
    expect(plan("What was my bill in 2026-08?")).toMatchObject({ args: { month: "2026-08" } });
    expect(plan("What was my bill in September 2025?")).toMatchObject({ args: { month: "2025-09" } });
    expect(plan("What was my bill in September?")).toMatchObject({ args: { month: "september" } });
    expect(plan("May I see my AWS bill?")).toEqual(expect.objectContaining({ args: {} })); // "May" is not a month
  });
});

describe("named months in comparisons (audit #17)", () => {
  it.each([
    ["Compare my costs for August and September", "compare_monthly_costs", "2026-08", "2026-09"],
    ["Compare September 2026 with October 2026", "compare_monthly_costs", "2026-09", "2026-10"],
    ["Why did my September bill increase compared with August?", "investigate_cost_increase", "2026-08", "2026-09"],
    ["Compare September with October 2026", "compare_monthly_costs", "2026-09", "2026-10"],
    ["Compare December and January", "compare_monthly_costs", "2025-12", "2026-01"],
    ["Compare this month with last month", "compare_monthly_costs", "2026-09", "2026-10"],
    ["Compare my AWS costs with last month.", "compare_monthly_costs", "2026-09", "2026-10"],
    ["Why did my bill go up in September?", "investigate_cost_increase", "2026-08", "2026-09"],
  ])("%s -> %s %s..%s", (q, tool, earlier, later) => {
    expect(plan(q)).toEqual(expect.objectContaining({ kind: "call", tool,
                                                      args: { earlier_month: earlier, later_month: later } }));
  });

  it("with no month named, the tools' defaults apply", () => {
    expect(plan("Why did my AWS cost increase?")).toEqual(expect.objectContaining({ args: {} }));
  });

  it("more than two distinct months asks which two", () => {
    expect(plan("Compare July, August and September")).toMatchObject({ kind: "needs", missing: "months" });
  });

  it("monthsIn keeps the order named and reports the first invalid month", () => {
    expect(monthsIn("september and august").months.map((m) => m.kind === "name" && m.month)).toEqual([9, 8]);
    expect(monthsIn("2026-13 and 2026-09").invalid).toBe("2026-13");
  });
});

describe("forced refresh arguments (audit #27)", () => {
  const tool = (name: string, props: string[]): Tool =>
    ({ name, inputSchema: { type: "object", properties: Object.fromEntries(props.map((p) => [p, {}])) } }) as Tool;

  it("adds refresh: true only where the tool accepts it", () => {
    expect(withRefresh(tool("get_resource_inventory", ["region", "services", "refresh"]), { region: "ap-south-1" }))
      .toEqual({ region: "ap-south-1", refresh: true });
    expect(withRefresh(tool("scan_tag_compliance", ["refresh"]), {})).toEqual({ refresh: true });
    expect(withRefresh(tool("list_untagged_resources", ["limit", "cursor"]), { limit: 5 })).toEqual({ limit: 5 });
    expect(withRefresh(undefined, {})).toEqual({});
  });

  it("voice Refresh data asks the app for a refresh, not a cached answer", async () => {
    const execute = vi.fn<VoiceDeps["execute"]>(async () => ({ speech: "Fresh." }));
    const c = new VoiceController({
      createRecognition: null, synth: null, createUtterance: null, tools: () => ALL, needsConfirmation: () => false,
      lookup: () => ({ speech: "Cached.", ageMs: 5_000 }), execute,
    }, undefined, { autoListen: false });
    await c.handleTranscript("Check my tagging compliance.");
    expect(execute).not.toHaveBeenCalled(); // answered from the cache
    c.refresh();
    await new Promise((r) => setTimeout(r, 0));
    expect(execute).toHaveBeenCalledWith("scan_tag_compliance", {}, { refresh: true });
  });
});
