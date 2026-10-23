import { describe, expect, it } from "vitest";

import { mapIntent, monthIn } from "../src/lib/intent";

const ALL = ["get_monthly_aws_bill", "get_current_month_spending", "get_cost_breakdown", "compare_monthly_costs",
  "investigate_cost_increase", "get_cost_anomalies", "get_resource_inventory", "scan_tag_compliance",
  "list_untagged_resources", "explain_finding", "recommend_tags", "get_aws_account_identity"];

describe("mapIntent", () => {
  it.each([
    ["What was my AWS bill last month?", "get_monthly_aws_bill", { month: "last-month" }],
    ["How much have I spent this month?", "get_current_month_spending", {}],
    ["Which services are costing me the most?", "get_cost_breakdown", {}],
    ["Compare my AWS costs with last month.", "compare_monthly_costs", {}],
    ["Why did my AWS cost increase?", "investigate_cost_increase", {}],
    ["Explain why my bill went up", "investigate_cost_increase", {}],
    ["Are there any cost anomalies?", "get_cost_anomalies", {}],
    ["How many AWS resources do I have in Mumbai?", "get_resource_inventory", { region: "ap-south-1" }],
    ["Check my tagging compliance.", "scan_tag_compliance", {}],
    ["Show me untagged resources in Mumbai.", "list_untagged_resources", { region: "ap-south-1" }],
    ["What was my bill in 2026-08?", "get_monthly_aws_bill", { month: "2026-08" }],
    ["Explain finding tagcomp-0123456789abcdef", "explain_finding", { finding_id: "tagcomp-0123456789abcdef" }],
    ["Recommend tags for tagcomp-0123456789abcdef", "recommend_tags", { resource: "tagcomp-0123456789abcdef" }],
  ])("%s -> %s", (q, tool, args) => {
    const plan = mapIntent(q, ALL);
    expect(plan).toMatchObject({ kind: "call", tool, args });
  });

  it("asks for the finding instead of guessing", () => {
    expect(mapIntent("Explain this finding.", ALL)).toMatchObject({ kind: "needs", tool: "explain_finding", missing: "resource_name", args: {} });
    expect(mapIntent("Recommend tags for this resource.", ALL)).toMatchObject({ kind: "needs", tool: "recommend_tags", missing: "resource_name", args: {} });
  });

  it("only selects tools the server listed", () => {
    expect(mapIntent("Check my tagging compliance.", ["get_monthly_aws_bill"])).toMatchObject({ kind: "unavailable" });
    expect(mapIntent("approve the fix", ALL)).toMatchObject({ kind: "none" });
  });

  it("reports ambiguity between cost and governance", () => {
    const plan = mapIntent("how many resources and what is my bill", ALL);
    expect(plan.kind).toBe("ambiguous");
  });

  it("never invents a month", () => {
    expect(monthIn("what was my bill")).toBeUndefined();
    expect(mapIntent("what was my bill", ALL)).toMatchObject({ kind: "call", tool: "get_monthly_aws_bill", args: {} });
    expect(monthIn("bill for september")).toBe("september");
  });
});

// Listing tools ask which region to search before anything runs when the question names none.
const LISTING = new Set(["get_resource_inventory", "search_resources", "list_untagged_resources"]);
const routed = (tool: string) => (LISTING.has(tool) ? { kind: "needs", tool, missing: "region" } : { kind: "call", tool });

const COST = new Set(["get_monthly_aws_bill", "get_current_month_spending", "get_cost_breakdown", "compare_monthly_costs",
  "investigate_cost_increase", "get_cost_anomalies"]);

describe("routing by category", () => {
  const cases: [string, string, string[]][] = [
    ["last month's bill", "get_monthly_aws_bill", ["What was my AWS bill last month?", "How much did AWS charge me last month?",
      "Show last month's invoice", "What did I spend last month?", "last month's AWS cost"]],
    ["current month", "get_current_month_spending", ["How much have I spent this month?", "What is my spend so far?",
      "month to date AWS cost", "current month spending", "What's my bill this month?"]],
    ["cost increase", "investigate_cost_increase", ["Why did my AWS cost increase?", "Why did my bill go up?",
      "Investigate the cost increase", "Why is my spend higher than last month?", "why did costs rise"]],
    ["cost anomaly", "get_cost_anomalies", ["Are there any cost anomalies?", "Any unusual spend?", "Did my costs spike?",
      "Show anomalous charges"]],
    ["tag compliance", "scan_tag_compliance", ["Check my tagging compliance.", "Are my resources tagged properly?",
      "Run a tag compliance scan", "How compliant is my tag policy?"]],
    ["untagged", "list_untagged_resources", ["Show me untagged resources.", "Which resources are missing tags?",
      "List resources without tags", "What is not tagged?"]],
  ];
  for (const [label, tool, questions] of cases) {
    it.each(questions)(`${label}: %s`, (q) => {
      expect(mapIntent(q, ALL)).toMatchObject(routed(tool));
    });
  }

  it.each(["Explain this finding.", "Explain the finding please", "Why is this resource non-compliant?",
    "Why was it flagged?"])("explain asks for the finding: %s", (q) => {
    expect(mapIntent(q, ALL)).toMatchObject({ kind: "needs", tool: "explain_finding", missing: "resource_name" });
  });

  it.each(["Recommend tags for this resource.", "What tags should this resource have?", "recommend tag values"])(
    "recommend asks for the resource: %s", (q) => {
      expect(mapIntent(q, ALL)).toMatchObject({ kind: "needs", tool: "recommend_tags", missing: "resource_name" });
    });

  it.each([
    "What was my AWS bill last month?", "How much have I spent this month?", "Which services are costing me the most?",
    "Compare my AWS costs with last month.", "Why did my AWS cost increase?", "Are there any cost anomalies?",
    "Explain why my bill went up", "What did EC2 cost me?", "How much do Lambda functions cost?",
    "Which service costs the most?", "Explain the cost increase", "Why did my resource costs go up?",
    "cost of my S3 buckets", "What did I spend on DynamoDB tables?",
  ])("a cost question never routes to a governance tool: %s", (q) => {
    const plan = mapIntent(q, ALL);
    if (plan.kind === "call" || plan.kind === "needs") expect(COST.has(plan.tool)).toBe(true);
    else expect(["ambiguous", "none"]).toContain(plan.kind);
  });

  it("mixed questions ask for clarification instead of guessing", () => {
    expect(mapIntent("How many resources do I have and what is my bill?", ALL).kind).toBe("ambiguous");
    expect(mapIntent("hello", ALL).kind).toBe("none");
  });
});

describe("resource inventory questions", () => {
  it.each([
    "How many Lambdas do I have?",
    "List all the Lambdas?",
    "How many S3 buckets are there?",
    "Show my DynamoDB resources.",
    "How many EC2 instances are running?",
    "Count my lambda functions",
    "What buckets do I have?",
    "Do I have any DynamoDB tables?",
    "Give me an inventory of my AWS resources",
    "How many servers do I have?",
    "Which Lambda functions exist?",
    "Number of S3 buckets",
    "What resources are in my account?",
    "List my EC2 instances",
  ])("%s -> get_resource_inventory, asking which region first (no region named)", (q) => {
    const plan = mapIntent(q, ALL);
    expect(plan).toMatchObject({ kind: "needs", tool: "get_resource_inventory", missing: "region" });
    expect(plan.kind === "needs" && plan.prompt).toMatch(/^Which AWS region should I search/);
    expect(plan.kind === "needs" && "region" in plan.args).toBe(false); // never a default region
  });

  it("a listing question with a resource type gets names (no counts-only note); without one, says how to get names", () => {
    const plan = mapIntent("List all the Lambdas in Mumbai", ALL);
    expect(plan).toMatchObject({ kind: "call", tool: "get_resource_inventory", args: { services: ["lambda"] } });
    expect(plan.kind === "call" && plan.note).toBeFalsy();
    const generic = mapIntent("List all my resources in all regions", ALL);
    expect(generic.kind === "call" && generic.note).toMatch(/counts only/);
    const count = mapIntent("How many S3 buckets are there in all regions?", ALL);
    expect(count.kind === "call" && count.note).toBeFalsy();
  });

  it("never sends a listing question to list_untagged_resources", () => {
    for (const q of ["List all Lambda functions", "Show all my buckets", "list every DynamoDB table"]) {
      expect(mapIntent(q, ALL)).toMatchObject({ tool: "get_resource_inventory" });
    }
  });
});

describe("boundaries between inventory, tagging, findings and cost", () => {
  it.each([
    ["List Lambda functions without tags", "list_untagged_resources"],
    ["Which S3 buckets are missing tags?", "list_untagged_resources"],
    ["Show untagged DynamoDB tables", "list_untagged_resources"],
    ["Are my EC2 instances compliant?", "scan_tag_compliance"],
    ["Run a tag audit", "scan_tag_compliance"],
    ["How much do my Lambda functions cost?", "get_monthly_aws_bill"],
    ["What did I spend on S3 last month?", "get_monthly_aws_bill"],
    ["Which services cost the most?", "get_cost_breakdown"],
    ["What caused the bill to go up?", "investigate_cost_increase"],
    ["Has my spend spiked?", "get_cost_anomalies"],
    ["How much have I paid so far this month?", "get_current_month_spending"],
    ["Suggest tag values for tagcomp-0123456789abcdef", "recommend_tags"],
    ["Tell me about finding tagcomp-0123456789abcdef", "explain_finding"],
  ])("%s -> %s", (q, tool) => {
    expect(mapIntent(q, ALL)).toMatchObject(routed(tool));
  });

  it("an inventory word never pulls a cost question into governance", () => {
    for (const q of ["What did EC2 cost me?", "How much are my S3 buckets costing?", "DynamoDB tables price"]) {
      const plan = mapIntent(q, ALL);
      expect(plan.kind).toBe("call");
      expect(COST.has((plan as { tool: string }).tool)).toBe(true);
    }
  });
});

describe("unsupported and ambiguous questions", () => {
  it.each([
    "Delete my unused S3 buckets",
    "Terminate the idle EC2 instances",
    "Tag all my Lambdas with Owner=me",
    "Add tags to my DynamoDB tables",
    "Fix my untagged resources",
    "Approve the proposal",
  ])("refuses write requests (read-only): %s", (q) => {
    const plan = mapIntent(q, ALL);
    expect(plan).toMatchObject({ kind: "none" });
    // Tag changes get the approval-workflow answer; other writes are refused as read-only. Nothing runs either way.
    expect((plan as { reason: string }).reason).toMatch(/read-only|can't change tags from here/);
  });

  it.each(["What's the weather today?", "hello", "Tell me a joke", "Who are you?"])(
    "no tool for unrelated questions: %s", (q) => {
      expect(mapIntent(q, ALL)).toMatchObject({ kind: "none" });
    });

  it.each([
    "How many untagged Lambdas cost the most?",
    "Which non-compliant buckets are the most expensive?",
  ])("asks for clarification when cost and governance mix: %s", (q) => {
    expect(mapIntent(q, ALL).kind).toBe("ambiguous");
  });

  it("still asks for the finding instead of guessing", () => {
    expect(mapIntent("Explain this finding for my Lambda", ALL)).toMatchObject({ kind: "needs", tool: "explain_finding" });
    expect(mapIntent("Recommend tags for my S3 bucket", ALL)).toMatchObject({ kind: "needs", tool: "recommend_tags" });
  });

  it("respects what the server listed", () => {
    expect(mapIntent("How many Lambdas do I have?", ["get_monthly_aws_bill"])).toMatchObject({ kind: "unavailable" });
  });
});

describe("account identity", () => {
  it.each([
    "Which AWS account am I using?",
    "What account is CloudGuard connected to?",
    "Show my AWS account ID.",
    "what's my account number",
    "Which account are we using?",
    "Who am I logged in as?",
    "What AWS account is this?",
  ])("%s -> get_aws_account_identity", (q) => {
    expect(mapIntent(q, ALL)).toMatchObject({ kind: "call", tool: "get_aws_account_identity", args: {} });
  });

  it.each([
    ["How much did my AWS account cost last month?", "get_monthly_aws_bill"],
    ["Which account costs the most?", "get_cost_breakdown"],
    ["How many resources are in my account?", "get_resource_inventory"],
    ["Check my tagging compliance.", "scan_tag_compliance"],
  ])("%s is not an identity question", (q, tool) => {
    expect(mapIntent(q, ALL)).toMatchObject({ tool }); // inventory asks for its region first ("needs")
  });

  it("is unavailable when the server did not list the tool", () => {
    expect(mapIntent("Which AWS account am I using?", ALL.filter((t) => t !== "get_aws_account_identity")))
      .toMatchObject({ kind: "unavailable", tool: "get_aws_account_identity" });
  });
});
