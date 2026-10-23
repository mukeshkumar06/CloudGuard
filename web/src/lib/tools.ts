import type { Tool } from "@modelcontextprotocol/sdk/types.js";

// Grouping is presentation only: the list itself always comes from the server's tools/list.
const COST = /(bill|spending|cost)/;

export function group(tool: Tool): "Cost Intelligence" | "Resource Governance" | "Account" | "Other" {
  if (/identity/.test(tool.name)) return "Account";
  if (COST.test(tool.name)) return "Cost Intelligence";
  if (/(resource|tag|finding)/.test(tool.name)) return "Resource Governance";
  return "Other";
}

/** "read-only" only when the server says so in the tool's annotations. */
export function readOnly(tool: Tool): "read-only" | "not declared read-only" {
  return tool.annotations?.readOnlyHint === true && tool.annotations?.destructiveHint !== true
    ? "read-only" : "not declared read-only";
}

// CloudGuard's read-only tools: a question that maps to one of these runs at once, without "Shall I continue?".
// recommend_tags only suggests values; it never applies a tag.
export const READ_ONLY_TOOLS: ReadonlySet<string> = new Set([
  "get_aws_account_identity", "get_monthly_aws_bill", "get_current_month_spending", "get_cost_breakdown",
  "compare_monthly_costs", "investigate_cost_increase", "get_cost_anomalies", "get_resource_inventory",
  "search_resources", "scan_tag_compliance", "list_untagged_resources", "explain_finding", "recommend_tags",
]);

/** Fails closed: no confirmation only for a known read-only tool that the server also declares read-only.
 *  Anything else (an unknown tool, a missing or changed annotation) needs an explicit yes. */
export function requiresConfirmation(tool: Tool | undefined): boolean {
  return !(tool && READ_ONLY_TOOLS.has(tool.name) && readOnly(tool) === "read-only");
}

/** The arguments for a forced refresh: `refresh: true` added only when the server's schema for this tool takes it
 *  (get_resource_inventory, scan_tag_compliance), so other tools still get exactly the arguments asked for. */
export function withRefresh(tool: Tool | undefined, args: Record<string, unknown>): Record<string, unknown> {
  const props = (tool?.inputSchema as { properties?: Record<string, unknown> } | undefined)?.properties;
  return props && "refresh" in props ? { ...args, refresh: true } : args;
}

/** Whether a tool can search every enabled region, from its tools/list input schema: its `region` argument accepts
 *  "all", or it has no region argument (it always covers every enabled region). */
export function regionSupport(tool: Tool | undefined): { allRegions: boolean } {
  const props = (tool?.inputSchema as { properties?: Record<string, unknown> } | undefined)?.properties;
  const region = props?.region;
  if (!region) return { allRegions: true };
  // A plain string schema or an anyOf (string | null): look for "all" in its patterns or its description.
  const text = JSON.stringify(region);
  return { allRegions: text.includes("^(all|") || text.includes('\\"all\\"') };
}

export function title(tool: Tool): string {
  return tool.annotations?.title ?? tool.title ?? tool.name;
}

/** First line of the server's description. */
export function summary(tool: Tool): string {
  return (tool.description ?? "").split("\n")[0].trim();
}
