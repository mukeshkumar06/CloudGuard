// Resource discovery as a conversation: a listing request ("List all Lambda functions") never runs until the user
// has said which region to search. Deterministic; no model. Shared by the voice controller and the typed chat.
//
// Stages, in order (CONFIRM_INTERPRETATION only for spoken tagging requests, which are read back as heard first):
//   UNDERSTAND_REQUEST -> CONFIRM_INTERPRETATION -> COLLECT_REGION -> DISCOVER_RESOURCES -> DISPLAY_RESULTS
// A tagging request then continues in its own flow (tag, preview, confirmation, execution, verification), which
// keeps the region chosen here. The backend still discovers everything and checks the region is enabled.

import { nameFilterIn, servicesIn } from "./intent";
import { regionLabel, resolveRegion, resolveRegionAnswer, type RegionChoice } from "./regions";
import { filterWords, serviceLabel, type TagIntent } from "./tagflow";

export type DiscoveryStage = "UNDERSTAND_REQUEST" | "CONFIRM_INTERPRETATION" | "COLLECT_REGION" | "DISCOVER_RESOURCES"
  | "DISPLAY_RESULTS";

export const STAGE_LABEL: Record<DiscoveryStage, string> = {
  UNDERSTAND_REQUEST: "Understand", CONFIRM_INTERPRETATION: "Confirm", COLLECT_REGION: "Region",
  DISCOVER_RESOURCES: "Discover", DISPLAY_RESULTS: "Results",
};

/** One resource-discovery request across turns: what was asked, what it means, and where to search. */
export interface DiscoveryContext {
  id: number;
  stage: DiscoveryStage;
  request: string; // the user's original words, kept while the region is asked for
  tool: string; // the listing tool that will run (search_resources for a tagging request)
  args: Record<string, unknown>; // the tool arguments without a region
  services: string[]; // resource types named ([] = every supported type)
  filter?: { match: string; text: string }; // a name filter, if any
  region?: string; // set once known: a region code, or "all"
  tagIntent?: TagIntent; // a tagging request waiting for its region
}

// Tools that list or count resources per region. Each takes `region` ("all" or one code).
export const LISTING_TOOLS = new Set(["get_resource_inventory", "search_resources", "list_untagged_resources"]);

export function isListing(tool: string): boolean {
  return LISTING_TOOLS.has(tool);
}

/** A new context for a listing request (no region yet unless args carry one). */
export function discoveryFor(id: number, request: string, tool: string, args: Record<string, unknown>,
                             stage: DiscoveryStage): DiscoveryContext {
  const { region, ...rest } = args;
  const services = Array.isArray(rest.services) ? (rest.services as string[]) : [];
  const filter = typeof rest.text === "string" ? { match: String(rest.match ?? "contains"), text: rest.text } : undefined;
  return { id, stage, request, tool, args: rest, services, ...(filter ? { filter } : {}),
           ...(typeof region === "string" ? { region } : {}) };
}

/** The tool arguments for a region choice. search_resources covers every region when none is given. */
export function withRegion(tool: string, args: Record<string, unknown>, region: string): Record<string, unknown> {
  const { region: _old, ...rest } = args;
  if (region === "all") return tool === "search_resources" ? rest : { ...rest, region: "all" };
  return { ...rest, region };
}

function typesWords(services: string[]): string {
  return services.length ? services.map((s) => serviceLabel(s)).join(" and ") : "";
}

/** What is being searched, in words: "Lambda functions", "untagged S3 buckets", "your AWS resources". */
export function whatWords(tool: string, args: Record<string, unknown>): string {
  const services = Array.isArray(args.services) ? (args.services as string[]) : [];
  const types = typesWords(services);
  if (tool === "list_untagged_resources") return types ? `untagged ${types}` : "untagged resources";
  if (tool === "search_resources" && typeof args.text === "string") {
    return `${types || "resources"} with ${filterWords({ match: (args.match ?? "contains") as never, text: args.text })}`;
  }
  return types || "your AWS resources";
}

export function whereWords(region: string | undefined): string {
  return !region || region === "all" ? "all supported regions" : regionLabel(region);
}

/** "Searching Lambda functions in Asia Pacific (Mumbai)." Said before the call, from the arguments only. */
export function searchingText(tool: string, args: Record<string, unknown>): string {
  const region = typeof args.region === "string" ? args.region : tool === "search_resources" ? "all" : undefined;
  return `Searching ${whatWords(tool, args)} in ${whereWords(region)}.`;
}

/** The answer to "Which region?": a region, all regions (only where the tool supports it), or the question again. */
export type RegionAnswer = { kind: "region"; region: string } | { kind: "retry"; prompt: string };

export function regionAnswer(text: string, opts: { allRegions: boolean; oneRegionReason?: string }): RegionAnswer {
  const choice = resolveRegionAnswer(text);
  if (choice.kind === "region") return { kind: "region", region: choice.code };
  if (choice.kind === "all") {
    if (opts.allRegions) return { kind: "region", region: "all" };
    return { kind: "retry", prompt: `${opts.oneRegionReason ?? "I can only search one region at a time for that."} `
      + "Which region should I use? For example Mumbai or ap-south-1." };
  }
  return { kind: "retry", prompt: retryWords(choice) };
}

function retryWords(choice: RegionChoice): string {
  if (choice.kind === "ambiguous") {
    return `That could be ${choice.candidates.map(regionLabel).join(" or ")}. Which one?`;
  }
  return "I didn't recognise that region. Say a region such as Mumbai, Singapore or US East, a region code such as "
    + "ap-south-1, or all regions.";
}

const CHANGE_WORDS = /^\s*(?:(?:and|ok(?:ay)?|now|then|actually|no|instead)[\s,]+)*(?:(?:what|how) about|try|switch(?: it)? to|change(?: the)? region to|use|search|check|look in|show(?: me)?(?: (?:it|them|those|that))?|do(?: the same| it)?|same(?: thing)?|and)?\s*(?:in\s+|for\s+)?(.+?)\s*(?:instead|then|now|too|please)?\s*[?.!]*\s*$/i;

/** A reply that only names a region ("Mumbai", "What about Singapore?", "Try us-east-1 instead"), with no resource
 *  type, name filter or tagging words of its own. Such a reply answers an open region question, or re-runs the last
 *  listing in the new region; anything more is a new request. */
export function regionOnly(text: string): string | undefined {
  const t = text.trim();
  if (!t || t.split(/\s+/).length > 8) return undefined;
  if (servicesIn(t).length || nameFilterIn(t) || /\b(tag\w*|untagged|bill|costs?|spend\w*|account|resources?|inventory|assets?)\b/i.test(t)) return undefined;
  const m = t.match(CHANGE_WORDS);
  const place = m?.[1] ?? t;
  const choice = resolveRegionAnswer(place);
  if (choice.kind === "region") return choice.code;
  if (choice.kind === "all") return "all";
  return resolveRegion(t).kind === "ambiguous" ? "ambiguous" : undefined;
}
