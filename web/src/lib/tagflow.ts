// Conversational group tagging: "Find all Lambdas with codebridge in their names and tag them." Deterministic; no
// model. This is a conversation over two read-only server tools, never a tagging engine of its own:
//
//   1. search_resources finds the matching resources (the server's discovery: live or fixture, as the server says).
//   2. The key and value are asked for and repeated back with the service, region, filter and matches.
//   3. Only an explicit yes (confirm.ts: never "sounds good", silence or a qualified answer) for exactly the summary
//      just spoken runs preview_tag_changes, the server's dry run. Any new key or value starts a new summary, and the
//      old yes no longer applies.
//   4. The preview's `request` is handed to the operator. Saving, approving (a different person) and executing (not
//      the approver) stay in the authenticated CLI; nothing said here can approve or apply a tag.
//
// Every function here is pure: it returns the next flow state, what to say, and at most one read-only tool call.

import { parseConfirmation } from "./confirm";
import { regionLabel, regionRetry, resolveRegion, unknownPlace } from "./regions";
import { sayNames } from "./resources";

export type NameMatch = "contains" | "starts_with" | "ends_with" | "exact";
export type FlowStage = "searching" | "need_tags" | "confirm" | "previewing" | "ready" | "closed";

const SERVICE_WORDS: { service: string; label: string; one: string; words: RegExp }[] = [
  { service: "lambda", label: "Lambda functions", one: "Lambda function",
    words: /\b(lambdas?|lambda functions?|functions?)\b/i },
  { service: "s3", label: "S3 buckets", one: "S3 bucket", words: /\b(s3|buckets?)\b/i },
  { service: "dynamodb", label: "DynamoDB tables", one: "DynamoDB table", words: /\b(dynamo(db)?|ddb|tables?)\b/i },
  { service: "ec2", label: "EC2 instances", one: "EC2 instance", words: /\b(ec2|instances?)\b/i },
];

export interface TagIntent {
  service: string;
  filter: { match: NameMatch; text: string };
  region?: string; // named in the request; otherwise the selected region is used
  tag?: { key: string; value: string };
}

export interface Matches {
  total: number;
  names: string[]; // safe display names only (the server withholds the rest)
  withheld: number;
  complete: boolean; // false: some resources could not be listed, so matches may be missing
}

export interface PreviewOut {
  status: string;
  matched: number;
  changing: number;
  conflicts: number;
  request: unknown | null; // the server's saveable TaggingRequest, or null
}

export interface TagFlow {
  id: number;
  version: number; // bumped by every new key or value: a yes only counts for the summary of this version
  service: string;
  region: string;
  filter: { match: NameMatch; text: string };
  stage: FlowStage;
  mode?: string; // "live" or "fixture", as the server reported it
  matches?: Matches;
  tag?: { key: string; value: string };
  preview?: PreviewOut;
}

export interface FlowStep {
  flow: TagFlow;
  say: string;
  call?: { tool: "search_resources" | "preview_tag_changes"; args: Record<string, unknown> };
}

// ---------- parsing ----------

const TAG_VERB = /\btag(s|ging)?\b(?! (compliance|policy|report|status|audit|check))|\b(add|apply|put|set)\b.*\btags?\b/i;
// Name filters as people say them, including "with codebridge in their names".
const FILTERS: [RegExp, NameMatch][] = [
  [/\b(?:start(?:s|ing)?|begin(?:s|ning)?) with\s+["']?([\w.-]+)/i, "starts_with"],
  [/\bend(?:s|ing)? with\s+["']?([\w.-]+)/i, "ends_with"],
  [/\b(?:named|called) exactly\s+["']?([\w.-]+)/i, "exact"],
  [/\bwith\s+["']?([\w.-]+)["']?\s+in (?:their|the|its|his|her)?\s*names?\b/i, "contains"],
  [/\b(?:whose|where the|with) names? (?:contains?|includes?|has|have)\s+["']?([\w.-]+)/i, "contains"],
  // "Lambdas which has CodeBridge" (as people say it): a name containing the word.
  [/\b(?:which|that) (?:has|have)\s+(?:the (?:word|text|string|name) )?["']?([\w.-]+)/i, "contains"],
  [/\b(?:contain(?:s|ing)?|includ(?:e|es|ing)|matching|named like) (?:the (?:word|text|string) )?["']?([\w.-]+)/i,
   "contains"],
  // "Find all Lambda functions with CodeBridge and tag them": a bare "with X" only before "and ..." or "in ...", so
  // "tag them with Owner=x" or "tag my Lambdas with Environment." is never a name filter.
  [/\bwith\s+["']?([\w.-]+)["']?(?=\s+(?:and|in)\b|\s*,)/i, "contains"],
];
const KEY = String.raw`["']?([A-Za-z0-9_.:/+=@-]{1,128})["']?`;
const VAL = String.raw`["']?([\p{L}\p{N} _.:/+=@-]{1,256}?)["']?`;
const KV_PATTERNS = [
  new RegExp(String.raw`\bkey\s*(?:is|=|:|as|should be|of)?\s*${KEY}[\s,;.]*(?:and\s+)?(?:the\s+)?(?:tag\s+)?value\s*(?:is|=|:|as|should be|of)?\s*${VAL}\s*[.!?]*$`, "iu"),
  new RegExp(String.raw`\bvalue\s*(?:is|=|:|as|should be)?\s*${VAL}[\s,;.]+(?:and\s+)?(?:the\s+)?key\s*(?:is|=|:|as|should be)?\s*${KEY}\s*[.!?]*$`, "iu"),
  new RegExp(String.raw`(?:^|\s|\bwith\s+)${KEY}\s*=\s*${VAL}\s*[.!?]*$`, "iu"),
];
const CANCEL = /^\s*(cancel|stop|never ?mind|forget it|abort)\b/i;

function serviceOf(text: string) {
  const found = SERVICE_WORDS.filter((s) => s.words.test(text));
  return found.length === 1 ? found[0] : undefined;
}

export function serviceLabel(service: string, n = 2): string {
  const s = SERVICE_WORDS.find((x) => x.service === service);
  return s ? (n === 1 ? s.one : s.label) : service;
}

/** A tag key and value said in any of the supported ways ("Key is owner, value is mukesh", "owner=mukesh"), or
 *  undefined. The value is kept exactly as said (trimmed); nothing is guessed or re-cased. */
export function parseTagKeyValue(text: string): { key: string; value: string } | undefined {
  const t = text.trim();
  for (const [i, re] of KV_PATTERNS.entries()) {
    const m = t.match(re);
    if (!m) continue;
    const [key, value] = i === 1 ? [m[2], m[1]] : [m[1], m[2]];
    const v = value.trim();
    if (key && v) return { key, value: v };
  }
  return undefined;
}

/** Why a key or value can't be used (AWS limits, reserved prefixes), or undefined. The server checks again. */
export function tagProblem(tag: { key: string; value: string }): string | undefined {
  if (/^aws:/i.test(tag.key)) return "Keys starting with aws: are reserved by AWS.";
  if (/^cloudguard:/i.test(tag.key)) return "Keys starting with cloudguard: are reserved by CloudGuard.";
  if (tag.key.length > 128) return "A tag key can be at most 128 characters.";
  if (tag.value.length > 256) return "A tag value can be at most 256 characters.";
  return undefined;
}

/** A group tagging request ("Find all Lambdas with codebridge in their names and tag them"), or undefined when the
 *  question is not one: it must ask to tag, name exactly one resource type and give a name filter. */
export function tagIntentIn(question: string): TagIntent | { problem: string } | undefined {
  const q = question.trim();
  if (!TAG_VERB.test(q) || /\b(untagged|un tagged|missing|compliance|recommend|suggest|why)\b/i.test(q)) return undefined;
  let filter: TagIntent["filter"] | undefined;
  let rest = q;
  for (const [re, match] of FILTERS) {
    const m = q.match(re);
    const text = m?.[1]?.replace(/[.,!?]+$/, "");
    if (text && !/^(the|my|our|their|a|an|tags?|names?)$/i.test(text)) {
      filter = { match, text };
      rest = q.replace(m![0], " ");
      break;
    }
  }
  const svc = serviceOf(rest);
  if (!filter || !svc) return undefined;
  const intent: TagIntent = { service: svc.service, filter };
  const tag = parseTagKeyValue(rest);
  if (tag) intent.tag = tag;
  const region = resolveRegion(rest);
  if (region.kind === "region") intent.region = region.code;
  else if (region.kind === "all") return { problem: "Tagging is done one region at a time. Which region should I use?" };
  else if (region.kind === "ambiguous") return { problem: regionRetry(region) };
  else if (unknownPlace(rest)) return { problem: regionRetry(region) };
  return intent;
}

// ---------- speech ----------

export function filterWords(f: TagFlow["filter"]): string {
  const how = { contains: "containing", starts_with: "starting with", ends_with: "ending with", exact: "exactly" }[f.match];
  return `names ${how} ${f.text}`;
}

function whereWords(region: string): string {
  return `${regionLabel(region)}, ${region}`;
}

function matchWords(flow: TagFlow): string {
  const m = flow.matches!;
  const names = m.names.length ? `: ${sayNames(m.names, 10)}` : "";
  const withheld = m.withheld ? ` (${m.withheld} with names I can't read out)` : "";
  return `${m.total} ${serviceLabel(flow.service, m.total)}${names}${withheld}`;
}

/** The complete operation, repeated before every confirmation. */
export function summary(flow: TagFlow): string {
  return `Here is the complete operation. Service: ${serviceLabel(flow.service)}. Region: ${whereWords(flow.region)}. `
    + `Name filter: ${filterWords(flow.filter)}. Matched: ${matchWords(flow)}. Tag key: ${flow.tag!.key}. `
    + `Tag value: ${flow.tag!.value}. Shall I prepare this tagging proposal? Say yes or no.`;
}

const ASK_TAGS = "What tag key and value should I add? For example, say: key is owner, value is mukesh.";
const NOTHING_WRITTEN = "Nothing was written.";

// ---------- steps ----------

/** Start a flow: search first. The region is the one named, else the selected region. */
export function startTagFlow(intent: TagIntent, id: number, selectedRegion: string): FlowStep {
  const flow: TagFlow = { id, version: 1, service: intent.service, region: intent.region ?? selectedRegion,
                          filter: intent.filter, stage: "searching", ...(intent.tag ? { tag: intent.tag } : {}) };
  return { flow, say: `Searching for ${serviceLabel(flow.service)} in ${regionLabel(flow.region)} with `
                        + `${filterWords(flow.filter)}.`,
           call: { tool: "search_resources", args: { text: flow.filter.text, match: flow.filter.match,
                                                     services: [flow.service], region: flow.region, limit: 100 } } };
}

type SearchOut = { mode?: string; total?: number; coverage_complete?: boolean;
                   resources?: { display_name?: string | null; name_withheld?: boolean }[] };

/** The search answer: no matches ends the flow; otherwise ask for the tag, or confirm the one already given. */
export function onSearchResult(flow: TagFlow, sc: SearchOut | undefined, errorText?: string): FlowStep {
  if (!sc || errorText || typeof sc.total !== "number") {
    return { flow: { ...flow, stage: "closed" },
             say: `Sorry, the search didn't work${errorText ? `: ${errorText}` : ""}. ${NOTHING_WRITTEN}` };
  }
  const rows = sc.resources ?? [];
  const names = rows.map((r) => r.display_name).filter((n): n is string => typeof n === "string" && n.length > 0);
  const matches: Matches = { total: sc.total, names, withheld: Math.max(0, sc.total - names.length),
                             complete: sc.coverage_complete !== false };
  const next: TagFlow = { ...flow, mode: sc.mode, matches };
  const source = sc.mode === "fixture" ? "Fixture mode, synthetic demo data, not your AWS account: "
    : sc.mode === "live" ? "From live discovery of your AWS account: " : "";
  const gap = matches.complete ? "" : " Some resources couldn't be listed, so matches may be missing.";
  if (sc.total === 0) {
    return { flow: { ...next, stage: "closed" },
             say: `${source}I found no ${serviceLabel(flow.service)} in ${regionLabel(flow.region)} with `
               + `${filterWords(flow.filter)}.${gap} There is nothing to tag. ${NOTHING_WRITTEN}` };
  }
  const found = `${source}I found ${matchWords(next)}, in ${regionLabel(flow.region)}.${gap}`;
  if (next.tag) return { flow: { ...next, stage: "confirm" }, say: `${found} ${summary(next)}` };
  return { flow: { ...next, stage: "need_tags" }, say: `${found} ${ASK_TAGS}` };
}

function withTag(flow: TagFlow, tag: { key: string; value: string }, prefix: string): FlowStep {
  const problem = tagProblem(tag);
  if (problem) return { flow: { ...flow, stage: "need_tags", tag: undefined }, say: `${problem} ${ASK_TAGS}` };
  // A new key or value is a new operation: a new version, so any earlier yes no longer applies.
  const next: TagFlow = { ...flow, tag, version: flow.version + 1, stage: "confirm", preview: undefined };
  return { flow: next, say: `${prefix}${summary(next)}` };
}

/** An answer while the flow waits for a key and value or for a yes or no. Never runs anything but the preview, and
 *  that only for an explicit yes to the current summary. `version`: the summary a button belongs to (stale
 *  buttons are ignored). */
export function onAnswer(flow: TagFlow, text: string, version = flow.version): FlowStep {
  if (CANCEL.test(text)) return { flow: { ...flow, stage: "closed" }, say: `Okay, cancelled. ${NOTHING_WRITTEN}` };
  const tag = parseTagKeyValue(text);
  if (flow.stage === "need_tags") {
    if (tag) return withTag(flow, tag, "");
    return { flow, say: `I didn't catch a key and a value. ${ASK_TAGS} Or say cancel.` };
  }
  if (flow.stage !== "confirm") return { flow, say: "" };
  if (tag) return withTag(flow, tag, "Okay, I've changed the tag. ");
  if (version !== flow.version) return { flow, say: "" }; // a confirmation for an older summary
  const { answer } = parseConfirmation(text);
  if (answer === "yes") {
    return { flow: { ...flow, stage: "previewing" },
             say: "Okay. Preparing the proposal preview. Nothing is applied by this step.",
             call: { tool: "preview_tag_changes",
                     args: { tags: { [flow.tag!.key]: flow.tag!.value }, service: flow.service,
                             name_match: flow.filter.match, name_text: flow.filter.text, region: flow.region } } };
  }
  if (answer === "no") {
    return { flow: { ...flow, stage: "need_tags", tag: undefined, version: flow.version + 1 },
             say: `Okay. ${NOTHING_WRITTEN} What key and value should I use instead? Or say cancel.` };
  }
  return { flow, say: `I need a clear yes or no. ${summary(flow)}` };
}

type PreviewSc = { status?: string; matched?: number; changing?: number; conflicts?: number; speech?: string;
                   request?: unknown | null; request_withheld?: boolean };

/** The preview answer: the request to hand to the operator, or why there is none. */
export function onPreviewResult(flow: TagFlow, sc: PreviewSc | undefined, errorText?: string): FlowStep {
  if (!sc || errorText || typeof sc.status !== "string") {
    return { flow: { ...flow, stage: "closed" },
             say: `Sorry, the preview didn't work${errorText ? `: ${errorText}` : ""}. ${NOTHING_WRITTEN}` };
  }
  const preview: PreviewOut = { status: sc.status, matched: sc.matched ?? 0, changing: sc.changing ?? 0,
                                conflicts: sc.conflicts ?? 0, request: sc.request ?? null };
  const next: TagFlow = { ...flow, preview };
  const server = sc.speech ? `${sc.speech} ` : "";
  if (sc.status === "PROPOSAL_READY" && preview.request) {
    const drift = flow.matches && preview.matched !== flow.matches.total
      ? ` Note: the search found ${flow.matches.total}, but the preview matched ${preview.matched}. Check the list `
        + "before saving." : "";
    return { flow: { ...next, stage: "ready" },
             say: `${server}The proposal request is ready on screen.${drift} Nothing has been applied. This voice `
               + "client can't approve or apply tags. To apply it, an operator saves the proposal from that "
               + "request, a different person approves its content hash, and someone who is not the approver "
               + "executes it. Execution re-checks and verifies every resource and reports each result." };
  }
  if (sc.status === "PROPOSAL_READY") {
    return { flow: { ...next, stage: "closed" },
             say: `${server}The request contains text I can't pass on safely, so no request was prepared. `
               + NOTHING_WRITTEN };
  }
  if (sc.status === "NO_CHANGES" && preview.conflicts > 0) {
    // An add never overwrites: offer another key or value instead.
    return { flow: { ...next, stage: "need_tags", tag: undefined, version: flow.version + 1 },
             say: `${server}Say a different key and value, or cancel.` };
  }
  return { flow: { ...next, stage: "closed" }, say: `${server}${NOTHING_WRITTEN}` };
}

/** True while the flow takes the user's next answer. */
export function awaitingAnswer(flow: TagFlow | null | undefined): flow is TagFlow {
  return !!flow && (flow.stage === "need_tags" || flow.stage === "confirm");
}

/** The CLI steps for the operator, after a ready preview. Fixed text; the request comes from the server. */
export function handoffCommands(flow: TagFlow): string[] {
  const cg = "python -m cloudguard.governance proposal";
  // Fixture mode never reaches AWS: development identities stand in for the three people.
  const as = (who: string) => (flow.mode === "fixture" ? `--mode fixture --dev-identity ${who}` : "--mode live");
  return [
    `${cg} save --request-json request.json ${as("requester")}${flow.mode === "fixture" ? "" : ` --region ${flow.region}`}`,
    `${cg} approve PROPOSAL_ID --content-hash HASH ${as("approver")}   # a different person`,
    `${cg} execute PROPOSAL_ID --approval-id APPROVAL_ID ${as("executor")}   # not the approver`,
    `${cg} execution-show PROPOSAL_ID EXECUTION_ID   # each resource's verified result`,
  ];
}
