// Deterministic intent mapping: keyword rules choose among the tools the server actually listed. No LLM, no network
// call, no guessing of identifiers. Every plan is shown to the user; read-only tools run at once.

import { REGION_QUESTION, regionQuestion, regionRetry, resolveRegion, resolveRegionAnswer, unknownPlace } from "./regions";
import { resourceIn, whichResourcePrompt } from "./resources";
import { tagIntentIn, type TagIntent } from "./tagflow";

export type Plan =
  | { kind: "call"; tool: string; args: Record<string, unknown>; reason: string; note?: string }
  | { kind: "needs"; tool: string; args: Record<string, unknown>; missing: string; reason: string; prompt: string }
  | { kind: "ambiguous"; candidates: string[]; reason: string }
  | { kind: "unavailable"; tool: string; reason: string }
  // A group tagging conversation (tagflow.ts): search, ask for the tag, confirm, preview. Never a write.
  | { kind: "tag"; intent: TagIntent; reason: string }
  | { kind: "none"; reason: string };

// ---------- vocabulary ----------

const COST_WORDS = /\b(bills?|billing|invoices?|spend\w*|spent|costs?|costing|charged?|charges|pay(ing)?|paid|price|pricing|money|dollars?|expensive|cheap\w*)\b/i;
const TAG_WORDS = /\b(tag\w*|untagged|labels?|unlabel+ed|complian\w*|non-?compliant|findings?|tagcomp-[0-9a-f]+)\b/i;

// Supported resource types, with the words people use for them (singular and plural).
const SERVICES: { service: string; label: string; words: RegExp }[] = [
  { service: "lambda", label: "Lambda functions", words: /\b(lambdas?|lambda functions?|functions?|serverless)\b/i },
  { service: "s3", label: "S3 buckets", words: /\b(s3|buckets?|s3 buckets?)\b/i },
  { service: "dynamodb", label: "DynamoDB tables", words: /\b(dynamo(db)?|ddb|tables?)\b/i },
  { service: "ec2", label: "EC2 instances", words: /\b(ec2|instances?|servers?|vms?|virtual machines?)\b/i },
];
const GENERIC_RESOURCES = /\b(resources?|inventory|assets?|everything|stuff|infrastructure|services i (have|use|run))\b/i;
const COUNT_OR_LIST = /\b(how many|count|number of|total|list|show|find|display|enumerate|give me|what|which|see|are there|do i have|i have|inventory|names?|all (my|the|of))\b/i;
const LISTING = /\b(list|show|display|enumerate|names?|which|what are|all (my|the|of))\b/i;

// Write requests: CloudGuard is read-only, so these are explained, never routed to a tool.
const WRITE = /\b(delete|remove|terminate|shut ?down|create|deploy|fix|remediate|approve|execute|clean ?up)\b|\b(apply|add|set|put|update|change)\b.*\btags?\b|\btag (my|all|the|these|those|it|them)\b/i;
// Requests to change tags without saying "tag": "Add Environment as Production to all three", "Apply the approved
// changes", "Only update the production ones".
const TAG_CHANGE = /\b(add|set|put|apply|update|change|assign)\b.*\b(environment|owner|project|cost ?center|team|tags?|changes?|values?|ones|them|those|these)\b|\b(show|preview|see)\b.*\b(the )?(changes?|proposal|tag changes?)\b/i;

export function servicesIn(question: string): string[] {
  return SERVICES.filter((s) => s.words.test(question)).map((s) => s.service);
}

function isInventoryQuestion(q: string): boolean {
  if (TAG_WORDS.test(q)) return false;
  if (/\b(check|scan|search|show|look (at|in))\b.*\b((all|every|each) (the |my |aws )?regions?|everywhere|globally)\b/i.test(q)
      && !COST_WORDS.test(q)) return true; // "Check every region"
  // "How much do my Lambdas cost?" is a cost question; "How many Lambdas ... and my bill?" is mixed (-> ambiguous).
  if (COST_WORDS.test(q) && !/\b(how many|count|number of)\b/i.test(q)) return false;
  const mentionsResources = GENERIC_RESOURCES.test(q) || servicesIn(q).length > 0;
  return mentionsResources && COUNT_OR_LIST.test(q);
}

// ---------- rules ----------

interface Rule {
  tool: string;
  test: (q: string) => boolean;
  label: string;
}

const re = (r: RegExp) => (q: string) => r.test(q);

// Order matters: more specific rules first. Governance rules come before cost rules, but a governance rule and a cost
// rule matching together is reported as ambiguous (see mapIntent).
const RULES: Rule[] = [
  { tool: "get_aws_account_identity", label: "AWS account identity",
    test: (q) => !COST_WORDS.test(q) && (
      /\b(account (id|ids|number|details?|info(rmation)?)|(which|what('?s| is)?( my)?) (aws )?account\b(?! (costs?|spends?|spent|is (the )?most))|account am i|who am i|caller identity|(logged|signed) in as)\b/i.test(q)
      || /\b((which|what) (aws |iam )?(identity|role|user|principal)\b|(aws|iam) (identity|role|user)\b.*\b(am i|are you|using|connected)\b|(identity|role|principal) (am i|are you)\b)/i.test(q)) },
  { tool: "recommend_tags", label: "tag recommendations",
    test: re(/\b(recommend|suggest|propose)\w*\b.*\b(tags?|tag values?|values?)\b|\b(what|which) (tags?|tag values?|values?)\b.*\b(should|do i need|to use|to add)\b/i) },
  { tool: "explain_finding", label: "explain a finding",
    test: (q) => /\b(explain|details?|tell me about|what'?s wrong with|describe)\b.*\b(finding|this resource|tagcomp-[0-9a-f]+)|\bwhy\b.*\b(non-?compliant|flagged|failing|failed)\b/i.test(q)
      || /\bwhy\b.*\b(untagged|un tagged|not tagged|missing\b.*\btags?|without tags|lacking\b.*\btags?)\b/i.test(q)
      || /\b(explain|describe)\b.*\b(tag(ging)? )?(issues?|problems?)\b/i.test(q)
      || (/\bwhat'?s wrong with\b/i.test(q) && !COST_WORDS.test(q)) },
  { tool: "list_untagged_resources", label: "untagged resources",
    test: (q) => /\b(untagged|un tagged|unlabel+ed|missing( (a|the|any|mandatory|required))? tags?|not tagged|without( any)? tags?|no tags|lack\w* tags?|(don'?t|do not) have tags?|have no tags|needs? tags?|need tagging)\b/i.test(q)
      // Speech recognition often hears "untagged" as "unpacked": accepted only next to a resource word.
      || (/\bun-?packed\b/i.test(q) && (GENERIC_RESOURCES.test(q) || servicesIn(q).length > 0)) },
  { tool: "scan_tag_compliance", label: "tag compliance",
    test: re(/\b(complian\w*|non-?compliant|tag(ging)? (check|audit|scan|status|health|report|policy)|tagged (properly|correctly)|properly tagged|correctly tagged|invalid (tags?|tag values?|values?)|(tag|tagging) (policy )?violations?|violat\w*\b.*\btag(ging)? polic\w*|(incorrect|wrong|bad|invalid) (tags?|tag values?)|tag(ging)? (rules?|problems?|issues?|errors?|mistakes?)|(required|mandatory) tags)\b/i) },
  { tool: "search_resources", label: "resource name search", test: (q) => nameFilterIn(q) !== undefined },
  { tool: "get_resource_inventory", label: "resource inventory", test: isInventoryQuestion },
  { tool: "get_cost_anomalies", label: "cost anomalies",
    test: re(/\banomal(y|ies|ous)\b|\b(unusual|unexpected|abnormal|strange|weird)\b.*\b(spend\w*|costs?|charges?|bills?)\b|\bspikes?\b|\bspiked?\b/i) },
  { tool: "investigate_cost_increase", label: "investigate a cost increase",
    test: re(/\b(why|investigate|explain|what (caused|drove|made))\b.*\b(costs?|bills?|spend\w*|charges?)\b.*\b(increase\w*|went up|go(ne)? up|rise|rose|risen|higher|jump\w*|grow\w*|grew|so high)\b|\bcost increase\b/i) },
  { tool: "compare_monthly_costs", label: "compare months",
    test: re(/\bcompar\w*\b|\b(versus|vs\.?)\b|\bmonth over month\b|\bdifference between\b.*\bmonths?\b/i) },
  { tool: "get_cost_breakdown", label: "cost breakdown",
    test: (q) => /\b(costing me the most|most expensive|costs? the most|biggest (cost|spend\w*)|top (costs?|spend\w*)|breakdown|break ?down)\b/i.test(q)
      || (COST_WORDS.test(q) && /\b(top services?|by service|per service|which services?|what services?)\b/i.test(q)) },
  { tool: "get_current_month_spending", label: "current month spending",
    test: (q) => COST_WORDS.test(q) && /\b(this month|so far|month to date|mtd|current month|right now|to date)\b/i.test(q) },
  { tool: "get_monthly_aws_bill", label: "monthly bill", test: re(COST_WORDS) },
];

const GOV = new Set(["get_aws_account_identity", "recommend_tags", "explain_finding", "list_untagged_resources", "scan_tag_compliance",
  "get_resource_inventory", "search_resources"]);

const MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
  "november", "december"];
const FINDING_ID = /\btagcomp-[0-9a-f]{16}\b/;
const ARN = /\barn:aws[a-z-]*:(lambda|dynamodb|s3|ec2):[a-z0-9-]*:(\d{12})?:\S{1,1024}/;

export const TAG_CHANGE_REFUSAL = "I can't change tags from here, and saying yes here never applies anything. "
  + "Tag changes go through an approval: an operator previews and saves a proposal, a second person approves "
  + "exactly that proposal, and only then is it applied. Nothing has been changed.";

// Name filters ("Lambdas containing ecommerce", "tables starting with user"). Tag filters ("tagged with", "with
// tag") are never name filters.
const NAME_FILTERS: [RegExp, "contains" | "starts_with" | "ends_with" | "exact"][] = [
  [/\b(?:start(?:s|ing)?|begin(?:s|ning)?) with\s+["']?([\w.-]+)/i, "starts_with"],
  [/\bend(?:s|ing)? with\s+["']?([\w.-]+)/i, "ends_with"],
  [/\b(?:contain(?:s|ing)?|includ(?:e|es|ing)) (?:the (?:word|text|string) )?["']?([\w.-]+)/i, "contains"],
  [/\b(?:named|called) exactly\s+["']?([\w.-]+)/i, "exact"],
];

/** The name filter a question asks for, if it is a resource question with one. */
export function nameFilterIn(question: string): { match: string; text: string } | undefined {
  if (/\btag(?:ged|s)?\b/i.test(question)) return undefined;
  if (!(GENERIC_RESOURCES.test(question) || servicesIn(question).length > 0)) return undefined;
  for (const [re, match] of NAME_FILTERS) {
    const text = question.match(re)?.[1]?.replace(/[.,!?]+$/, "");
    if (text && !/^(the|my|our|their)$/i.test(text)) return { match, text };  // "starting with A" is a letter
  }
  return undefined;
}

/** search_resources arguments: the name filter, the services named and the region (every region by default). */
function searchPlan(q: string, reason: string): Plan {
  const filter = nameFilterIn(q)!;
  const args: Record<string, unknown> = { text: filter.text, match: filter.match };
  const services = servicesIn(q.replace(filter.text, " "));
  if (services.length && services.length < SERVICES.length) args.services = services;
  const region = resolveRegion(q.replace(filter.text, " "));
  if (region.kind === "region") args.region = region.code;
  else if (region.kind === "all") { /* every region: search_resources' own default */ }
  else if (region.kind === "ambiguous" || unknownPlace(q)) {
    return { kind: "needs", tool: "search_resources", args, missing: "region", reason,
             prompt: region.kind === "ambiguous" ? regionRetry(region) : `${regionRetry(region)} ${REGION_QUESTION}` };
  } else return askRegion("search_resources", args, reason);
  return { kind: "call", tool: "search_resources", args, reason };
}

/** A listing request that names no region: ask which region before any discovery runs (never a default). */
function askRegion(tool: string, args: Record<string, unknown>, reason: string): Plan {
  const services = Array.isArray(args.services) ? (args.services as string[]) : [];
  const what = services.length === 1 ? SERVICES.find((s) => s.service === services[0])?.label : undefined;
  return { kind: "needs", tool, args, missing: "region", reason, prompt: regionQuestion(what) };
}

/** A month argument only when the question names one explicitly; never a guess. */
export function monthIn(question: string): string | undefined {
  const q = question.toLowerCase();
  const iso = q.match(/\b(20\d{2}-(0[1-9]|1[0-2]))\b/);
  if (iso) return iso[1];
  if (/\blast month\b|\bprevious month\b/.test(q)) return "last-month";
  if (/\bthis month\b/.test(q)) return "this-month";
  const name = MONTHS.find((m) => new RegExp(`\\b${m}\\b`).test(q));
  return name;
}

/** A month the question names, in the order named: YYYY-MM, "September 2026", "September", last/this month. */
type MonthRef = { kind: "iso"; year: number; month: number } | { kind: "name"; month: number; year?: number }
  | { kind: "relative"; offset: 0 | -1 };

/** Every month named in the question, or the first invalid one ("2026-13"). Never a guess. */
export function monthsIn(question: string): { months: MonthRef[]; invalid?: string } {
  const q = question.toLowerCase();
  const found: { at: number; ref: MonthRef }[] = [];
  for (const m of q.matchAll(/\b(\d{4})-(\d{1,2})\b/g)) {
    const year = Number(m[1]);
    const month = Number(m[2]);
    if (month < 1 || month > 12 || year < 2000) return { months: [], invalid: m[0] };
    found.push({ at: m.index!, ref: { kind: "iso", year, month } });
  }
  const names = new RegExp(`\\b(${MONTHS.join("|")})\\b(?:,?\\s+(\\d{4}))?`, "g");
  for (const m of q.matchAll(names)) {
    // "may" is also a verb ("May I see..."): only a month with a year, or after "in"/"for"/"and"/"with"/"of".
    if (m[1] === "may" && !m[2] && !/\b(in|for|and|with|of|vs|versus)\s+$/.test(q.slice(0, m.index))) continue;
    found.push({ at: m.index!, ref: { kind: "name", month: MONTHS.indexOf(m[1]) + 1,
                                      ...(m[2] ? { year: Number(m[2]) } : {}) } });
  }
  for (const m of q.matchAll(/\b(last|previous|this|current) month\b/g)) {
    found.push({ at: m.index!, ref: { kind: "relative", offset: m[1] === "last" || m[1] === "previous" ? -1 : 0 } });
  }
  return { months: found.sort((a, b) => a.at - b.at).map((f) => f.ref) };
}

/** An invalid explicit month ("2026-13") is asked about, never replaced by the default month. */
function invalidMonth(tool: string, invalid: string, reason: string): Plan {
  return { kind: "needs", tool, args: {}, missing: "month", reason,
           prompt: `${invalid} isn't a valid month. Which month do you mean? Say a month name such as September, `
             + "or use YYYY-MM." };
}

const iso = (y: number, m: number) => `${y}-${String(m).padStart(2, "0")}`;
const index = (y: number, m: number) => y * 12 + (m - 1);
const fromIndex = (i: number) => iso(Math.floor(i / 12), (i % 12) + 1);

/** Month refs to absolute month indexes. A name without a year is its latest occurrence not after `today` (as on
 *  the server), unless another named month gives the year. */
function resolveMonths(refs: MonthRef[], today: Date): number[] {
  const now = index(today.getFullYear(), today.getMonth() + 1);
  const explicitYear = refs.find((r) => r.kind !== "relative" && r.year !== undefined) as { year: number } | undefined;
  return refs.map((r) => {
    if (r.kind === "relative") return now + r.offset;
    if (r.kind === "iso") return index(r.year, r.month);
    if (r.year !== undefined) return index(r.year, r.month);
    if (explicitYear) return index(explicitYear.year, r.month);
    const thisYear = index(today.getFullYear(), r.month);
    return thisYear <= now ? thisYear : thisYear - 12;
  });
}

/** compare_monthly_costs / investigate_cost_increase months: both explicit (YYYY-MM, earlier first), or none when
 *  the question names none (the tools' default: the last two completed months). One named month is compared with
 *  the month before it; "last month" alone with this month. */
function comparisonPlan(tool: string, q: string, reason: string, today: Date): Plan {
  const { months, invalid } = monthsIn(q);
  if (invalid) return invalidMonth(tool, invalid, reason);
  if (months.length === 0) return { kind: "call", tool, args: {}, reason };
  const resolved = [...new Set(resolveMonths(months, today))].sort((a, b) => a - b);
  let pair: [number, number];
  if (resolved.length >= 2) pair = [resolved[0], resolved[resolved.length - 1]];
  else if (months.length === 1 && months[0].kind === "relative" && months[0].offset === -1) {
    pair = [resolved[0], resolved[0] + 1];
  } else pair = [resolved[0] - 1, resolved[0]];
  if (months.length > 2 && resolved.length > 2) {
    return { kind: "needs", tool, args: {}, missing: "months", reason,
             prompt: "That names more than two months. Which two months should I compare?" };
  }
  return { kind: "call", tool, args: { earlier_month: fromIndex(pair[0]), later_month: fromIndex(pair[1]) }, reason };
}

function inventoryNote(q: string): string | undefined {
  if (!LISTING.test(q)) return undefined;
  const named = SERVICES.filter((s) => s.words.test(q));
  // With a service named, the inventory answer includes the resources' safe display names (and they are read out).
  if (named.length) return undefined;
  return "Without a resource type, get_resource_inventory reports counts only. Name one (\"list my Lambda functions\") "
    + "to hear the resources' names.";
}

/** The answer to a pending "which resource?" question: the call with that name. A generic answer ("it", "that
 *  one") asks again; the server resolves the name, so nothing here guesses. */
export function answerResourceName(pending: { tool: string; args: Record<string, unknown>; reason: string },
                                   answer: string): Plan {
  const cleaned = answer.trim().replace(/^(the|my|our)\s+/i, "").replace(/[?.!,]+$/, "").trim();
  const named = resourceIn(`explain ${cleaned}`);
  const service = (pending.args.service as string | undefined) ?? named.service;
  if (!named.name) {
    return { kind: "needs", tool: pending.tool, args: pending.args, missing: "resource_name", reason: pending.reason,
             prompt: whichResourcePrompt(pending.tool === "explain_finding" ? "explain" : "recommend", service) };
  }
  return { kind: "call", tool: pending.tool, reason: pending.reason,
           args: { ...pending.args, ...(service ? { service } : {}), resource_name: named.name } };
}

/** The answer to a pending "which region?" question: the call with that region, or the same question again (with
 *  a retry prompt) when the answer names no usable region. Never treated as a new question. */
export function answerRegion(pending: { tool?: string; args: Record<string, unknown>; reason: string },
                             answer: string, opts: { allRegions?: boolean } = {}): Plan {
  const tool = pending.tool ?? "get_resource_inventory";
  const region = resolveRegionAnswer(answer);
  if (region.kind === "all" && opts.allRegions === false) {
    return { kind: "needs", tool, args: pending.args, missing: "region", reason: pending.reason,
             prompt: "This server can't search every region for that request. Which one region should I search? "
               + "For example Mumbai or ap-south-1." };
  }
  if (region.kind === "region" || region.kind === "all") {
    // search_resources covers every region when no region is given; inventory takes "all" explicitly.
    const where = region.kind === "region" ? { region: region.code } : tool === "search_resources" ? {} : { region: "all" };
    return { kind: "call", tool, reason: pending.reason, args: { ...pending.args, ...where } };
  }
  return { kind: "needs", tool, args: pending.args, missing: "region", reason: pending.reason, prompt: regionRetry(region) };
}

/** Inventory arguments for the region and services the question names. No region named is a "needs" plan that
 *  asks which region (or all regions) to search; a region that is named but unknown or ambiguous is asked about
 *  too. Nothing is discovered until the region is known. */
export function inventoryPlan(q: string, reason: string): Plan {
  const args: Record<string, unknown> = {};
  const services = servicesIn(q);
  if (services.length && services.length < SERVICES.length) args.services = services;
  const region = resolveRegion(q);
  const note = inventoryNote(q);
  if (region.kind === "region") args.region = region.code;
  else if (region.kind === "all") args.region = "all";
  else if (region.kind === "ambiguous" || unknownPlace(q)) {
    // A region was named but isn't one CloudGuard knows, or fits several: ask, never fall back to all regions.
    const prompt = region.kind === "ambiguous" ? regionRetry(region) : `${regionRetry(region)} ${REGION_QUESTION}`;
    return { kind: "needs", tool: "get_resource_inventory", args, missing: "region", reason, prompt };
  } else return askRegion("get_resource_inventory", args, reason); // no region named: ask, never assume one
  return note ? { kind: "call", tool: "get_resource_inventory", args, reason, note }
    : { kind: "call", tool: "get_resource_inventory", args, reason };
}

export function mapIntent(question: string, available: string[], today: Date = new Date()): Plan {
  const q = question.trim();
  if (!q) return { kind: "none", reason: "Ask a question about AWS costs, resources or tagging." };
  // "Find all Lambdas with codebridge in their names and tag them": the tagging conversation, when the server lists
  // the two read-only tools it uses. Without them (or for any other change request) the refusal below applies.
  const tagIntent = tagIntentIn(q);
  if (tagIntent && available.includes("search_resources") && available.includes("preview_tag_changes")) {
    if ("problem" in tagIntent) return { kind: "none", reason: tagIntent.problem };
    return { kind: "tag", intent: tagIntent, reason: "Matched \"group tagging\" by keywords (deterministic rules, "
      + "not an AI model). Searching first; nothing is written." };
  }
  const matches = RULES.filter((r) => r.test(q));
  const advisory = matches.some((m) => m.tool === "recommend_tags" || m.tool === "explain_finding");
  if ((WRITE.test(q) || (TAG_CHANGE.test(q) && !COST_WORDS.test(q))) && !advisory) {
    if (/\btag/i.test(q) || /\b(apply|approve)\b/i.test(q) || TAG_CHANGE.test(q)) {
      // Tag changes exist, but never from this client: its Alexa token can't write, and voice can't approve.
      return { kind: "none", reason: TAG_CHANGE_REFUSAL };
    }
    return { kind: "none", reason: "CloudGuard is read-only here: it can't change, tag, delete or fix resources. "
      + "Ask what is missing (untagged resources) or for tag recommendations instead." };
  }
  if (matches.length === 0) {
    return { kind: "none", reason: "No tool matches that question. Try asking about your bill, costs, resource counts, "
      + "tag compliance or untagged resources, or pick a tool from the sidebar." };
  }
  const best = matches[0];
  // Several cost rules matching is not ambiguous (the first, more specific, wins); a governance and a cost rule
  // matching together is, so the user chooses.
  const families = new Set(matches.map((m) => (GOV.has(m.tool) ? "gov" : "cost")));
  if (families.size > 1) {
    const candidates = [...new Set(matches.map((m) => m.tool))].filter((t) => available.includes(t));
    return { kind: "ambiguous", candidates, reason: "That could mean more than one thing; choose a tool." };
  }
  if (!available.includes(best.tool)) {
    return { kind: "unavailable", tool: best.tool, reason: `The server did not list ${best.tool} for this caller.` };
  }
  const reason = `Matched "${best.label}" by keywords (deterministic rules, not an AI model).`;
  const args: Record<string, unknown> = {};
  switch (best.tool) {
    case "get_monthly_aws_bill":
    case "get_cost_breakdown": {
      const { months, invalid } = monthsIn(q);
      if (invalid) return invalidMonth(best.tool, invalid, reason);
      const ref = months[0];
      const month = !ref ? undefined
        : ref.kind === "relative" ? (ref.offset === 0 ? "this-month" : "last-month")
        : ref.year !== undefined ? iso(ref.year, ref.month) : MONTHS[ref.month - 1];
      if (month === "this-month" && best.tool === "get_monthly_aws_bill" && available.includes("get_current_month_spending")) {
        return { kind: "call", tool: "get_current_month_spending", args: {}, reason };
      }
      if (month) args.month = month;
      return { kind: "call", tool: best.tool, args, reason };
    }
    case "compare_monthly_costs":
    case "investigate_cost_increase":
      // Named months are always passed explicitly; with none named, the tools' defaults (the last two completed
      // months) apply.
      return comparisonPlan(best.tool, q, reason, today);
    case "get_resource_inventory":
      return inventoryPlan(q, reason);
    case "search_resources":
      return searchPlan(q, reason);
    case "list_untagged_resources": {
      const services = servicesIn(q);
      if (services.length && services.length < SERVICES.length) args.services = services;
      const region = resolveRegion(q);
      if (region.kind === "region") args.region = region.code;
      else if (region.kind === "all") args.region = "all";
      else if (region.kind === "ambiguous" || unknownPlace(q)) {
        return { kind: "needs", tool: best.tool, args, missing: "region", reason,
                 prompt: region.kind === "ambiguous" ? regionRetry(region) : `${regionRetry(region)} ${REGION_QUESTION}` };
      } else return askRegion(best.tool, args, reason);
      return { kind: "call", tool: best.tool, args, reason };
    }
    case "explain_finding":
    case "recommend_tags": {
      // An ID or ARN still works, but ordinary questions name the resource ("Why is OrderProcessor untagged?").
      const ref = q.match(FINDING_ID)?.[0] ?? (best.tool === "recommend_tags" ? q.match(ARN)?.[0] : undefined);
      if (ref) {
        return { kind: "call", tool: best.tool, reason,
                 args: best.tool === "explain_finding" ? { finding_id: ref } : { resource: ref } };
      }
      const named = resourceIn(q);
      if (named.service) args.service = named.service;
      if (named.name) return { kind: "call", tool: best.tool, args: { resource_name: named.name, ...args }, reason };
      return { kind: "needs", tool: best.tool, args, missing: "resource_name", reason,
               prompt: whichResourcePrompt(best.tool === "explain_finding" ? "explain" : "recommend", named.service) };
    }
    default:
      return { kind: "call", tool: best.tool, args, reason };
  }
}
