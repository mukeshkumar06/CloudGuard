// Personal voice tagging, spoken only (see backend cloudguard/voice_tagging). Deterministic; no model. Pure steps,
// like tagflow.ts, whose parsing it reuses:
//
//   search (MCP search_resources, read-only) -> key and value -> prepare (server: live discovery, plan, a
//   confirmation challenge bound to the operator session) -> the complete operation repeated -> an explicit yes
//   (and, for overwrites, a separate "I confirm the overwrite") -> confirm (server: re-reads the transcript itself,
//   re-resolves from AWS, approves and executes through the existing workflow) -> per-resource results.
//
// Any new key or value cancels the pending challenge and prepares a new one, so an earlier yes never applies to
// it. The browser never decides on its own that a write is authorized: the server re-checks every transcript.

import { normalizeUtterance, parseConfirmation } from "./confirm";
import type { ConfirmOut, PrepareOut, PrepareRow, TagSpec } from "./operator";
import { regionLabel } from "./regions";
import { sayNames } from "./resources";
import { parseTagKeyValue, serviceLabel, tagProblem, type NameMatch, type TagIntent } from "./tagflow";

export type WriteStage = "searching" | "need_tags" | "preparing" | "confirm" | "overwrite_ack" | "conflict"
  | "executing" | "done" | "closed";

export interface WriteFlow {
  id: number;
  service: string;
  region: string;
  filter: { match: NameMatch; text: string };
  stage: WriteStage;
  tag?: { key: string; value: string };
  overwrite: boolean;
  found?: { total: number; names: string[]; complete: boolean };
  partial?: { key?: string; value?: string }; // half of a tag said so far ("key is owner"): only the rest is asked
  challenge?: string; // the server's challenge id for the summary just spoken
  requiresAck?: boolean;
  question?: string; // the last summary, repeated when an answer is unclear
  yes?: string; // the spoken yes, kept while the overwrite acknowledgement is asked
  mode?: string;
  plan?: PrepareOut; // the server's latest plan: resources, current values, changes, conflicts, overwrites
  results?: NonNullable<ConfirmOut["results"]>; // per-resource outcomes of the execution, from the server
  outcome?: string; // SUCCEEDED, PARTIAL, FAILED, ...
}

export type WriteCall =
  | { kind: "search"; args: Record<string, unknown> }
  | { kind: "prepare"; spec: TagSpec }
  | { kind: "confirm"; challenge: string; transcript: string; overwrite?: string };

export interface WriteStep {
  flow: WriteFlow;
  say: string;
  call?: WriteCall;
  cancel?: string; // a challenge to cancel on the server (fire and forget)
}

const NOTHING = "Nothing was written.";
const ASK_TAGS = "What tag key and value would you like to apply? For example, say: Project is CodeBridge.";
const KEY_RE = String.raw`["']?([A-Za-z0-9_.:/+=@-]{1,128})["']?`;
const VAL_RE = String.raw`["']?([\p{L}\p{N} _.:/+=@-]{1,256}?)["']?`;
const END = String.raw`\s*[.!]*$`;
// "Project is CodeBridge", "Use Environment as Development", "Set owner to Mukesh".
const LOOSE = new RegExp(String.raw`^\s*(?:please\s+)?(?:(?:use|set|make|add|apply)\s+)?(?:the\s+)?(?:tag\s+)?${KEY_RE}\s+(?:as|is|equals|=|to|should be)\s+${VAL_RE}${END}`, "iu");
const KEY_ONLY = new RegExp(String.raw`^\s*(?:the\s+)?(?:tag\s+)?key\s*(?:is|=|:|as|should be)?\s*${KEY_RE}${END}`, "iu");
const VALUE_ONLY = new RegExp(String.raw`^\s*(?:the\s+)?(?:tag\s+)?value\s*(?:is|=|:|as|should be)?\s*${VAL_RE}${END}`, "iu");
const BARE_KEY = new RegExp(String.raw`^${KEY_RE}${END}`, "u");
const BARE_VALUE = new RegExp(String.raw`^\s*${VAL_RE}${END}`, "u");
// Words that are never a tag key in a loose answer ("that is fine", "it is okay").
const NOT_A_KEY = /^(it|that|this|which|what|there|here|key|value|tag|the|yes|no|ok|okay|so|and|but|i|we|you)$/i;

/** A tag answer while waiting for one: both parts, or only the key or the value (then only the other is asked).
 *  Nothing is guessed: a bare word only fills the part that was asked for. */
export function parseTagAnswer(text: string, partial: WriteFlow["partial"] = {}):
    { tag: { key: string; value: string } } | { key: string } | { value: string } | undefined {
  const full = parseTagKeyValue(text);
  if (full) return { tag: full };
  const loose = text.match(LOOSE);
  if (loose && !NOT_A_KEY.test(loose[1]) && loose[2].trim()) return { tag: { key: loose[1], value: loose[2].trim() } };
  const k = text.match(KEY_ONLY)?.[1];
  const v = text.match(VALUE_ONLY)?.[1]?.trim();
  if (k && !NOT_A_KEY.test(k)) return partial.value ? { tag: { key: k, value: partial.value } } : { key: k };
  if (v) return partial.key ? { tag: { key: partial.key, value: v } } : { value: v };
  if (partial.key && !partial.value) {
    const bare = text.match(BARE_VALUE)?.[1]?.trim();
    if (bare && !/^(yes|no)$/i.test(bare)) return { tag: { key: partial.key, value: bare } };
  }
  if (partial.value && !partial.key) {
    const bare = text.match(BARE_KEY)?.[1];
    if (bare && !NOT_A_KEY.test(bare)) return { tag: { key: bare, value: partial.value } };
  }
  return undefined;
}
const CANCEL = /^\s*(cancel|stop|never ?mind|forget it|abort)\b/i;
const OVERWRITE_ACK = new Set(["i confirm the overwrite", "i confirm the overwrites", "confirm the overwrite",
  "confirm the overwrites", "confirm overwrite", "confirm overwrites", "yes overwrite them", "yes overwrite it",
  "overwrite them", "overwrite it", "yes i confirm the overwrite", "yes i confirm the overwrites"]);
const OVERWRITE_REQUEST = /^\s*(yes,?\s*)?(please\s+)?overwrite( (it|them|the (existing )?values?))?\.?\s*$/i;

function filterWords(f: WriteFlow["filter"]): string {
  if (f.match === "exact") return `the exact name ${f.text}`;
  const how = { contains: "containing", starts_with: "starting with", ends_with: "ending with" }[f.match];
  return `names ${how} ${f.text}`;
}

function spec(flow: WriteFlow): TagSpec {
  return { service: flow.service, region: flow.region, match: flow.filter.match, text: flow.filter.text,
           tags: { [flow.tag!.key]: flow.tag!.value }, ...(flow.overwrite ? { overwrite: true } : {}) };
}

function prepare(flow: WriteFlow, say: string, cancel?: string): WriteStep {
  const next: WriteFlow = { ...flow, stage: "preparing", challenge: undefined, requiresAck: false, yes: undefined };
  return { flow: next, say, call: { kind: "prepare", spec: spec(next) }, ...(cancel ? { cancel } : {}) };
}

/** Start: the read-only search, in the region named or the selected one. */
export function startWrite(intent: TagIntent, id: number, selectedRegion: string): WriteStep {
  const flow: WriteFlow = { id, service: intent.service, region: intent.region ?? selectedRegion,
                            filter: intent.filter, stage: "searching", overwrite: false,
                            ...(intent.tag ? { tag: intent.tag } : {}) };
  return { flow, say: "", call: { kind: "search", args: { text: flow.filter.text, match: flow.filter.match,
                                                          services: [flow.service], region: flow.region, limit: 100 } } };
}

type SearchOut = { mode?: string; total?: number; coverage_complete?: boolean; resources?: { display_name?: string | null }[] };

export function onWriteSearch(flow: WriteFlow, sc: SearchOut | undefined, errorText?: string): WriteStep {
  if (!sc || errorText || typeof sc.total !== "number") {
    return { flow: { ...flow, stage: "closed" }, say: `Sorry, the search didn't work. ${NOTHING}` };
  }
  const names = (sc.resources ?? []).map((r) => r.display_name).filter((n): n is string => !!n);
  const complete = sc.coverage_complete !== false;
  const next: WriteFlow = { ...flow, mode: sc.mode, found: { total: sc.total, names, complete } };
  // Disclosed before any tag is asked for or prepared: some resources could not be listed, so matches may be missing.
  const gap = complete ? "" : " Discovery was incomplete: some resources could not be listed, so there may be more matches I can't see.";
  const source = sc.mode === "fixture" ? "Fixture mode, simulated account, not your AWS account: "
    : sc.mode === "live" ? "From live discovery of your AWS account: " : "";
  if (sc.total === 0) {
    return { flow: { ...next, stage: "closed" }, say: `${source}I found no matching ${serviceLabel(flow.service)} in `
      + `${regionLabel(flow.region)} with ${filterWords(flow.filter)}.${gap} There is nothing to tag. ${NOTHING}` };
  }
  const found = `${source}I found ${sc.total} ${serviceLabel(flow.service, sc.total)} matching ${flow.filter.text} in `
    + `${regionLabel(flow.region)}${names.length ? `: ${sayNames(names, 10)}` : ""}.${gap}`;
  if (next.tag) return prepare(next, `${found} Checking the current tags.`);
  return { flow: { ...next, stage: "need_tags" }, say: `${found} ${ASK_TAGS}` };
}

function summary(flow: WriteFlow, out: PrepareOut): string {
  const rows = out.resources ?? [];
  const changing = rows.filter((r) => r.changes.length).map((r) => r.name);
  const already = rows.filter((r) => r.outcome === "ALREADY_MATCHING").map((r) => r.name);
  const unchanged = rows.filter((r) => !r.changes.length && r.outcome !== "ALREADY_MATCHING");
  const overwrites = out.overwrites ?? [];
  return `Here is the complete operation. Service: ${serviceLabel(flow.service)}. Region: ${regionLabel(flow.region)}, `
    + `${flow.region}. Name filter: ${filterWords(flow.filter)}. Matched: ${rows.length} `
    + `${serviceLabel(flow.service, rows.length)}${rows.length ? `: ${sayNames(rows.map((r) => r.name), 10)}` : ""}. `
    + `To change: ${changing.length}${already.length ? `; already correct: ${sayNames(already, 10)}` : ""}. `
    + (unchanged.length ? `Left unchanged: ${unchanged.map((r) => `${r.name}, ${unchangedReason(r)}`).join("; ")}. ` : "")
    + `Tag key: ${flow.tag!.key}. Tag value: ${flow.tag!.value}.`
    + (overwrites.length ? ` This overwrites existing values: ${overwrites.map((o) =>
      `${o.name}, ${o.key} from ${o.old_value} to ${o.new_value}`).join("; ")}.` : "")
    + ` ${out.mode === "fixture" ? "This is the simulated fixture account. " : ""}Shall I apply it now? Say yes or no.`;
}

/** Why a matched resource gets no change, in words (from the server's plan). */
export function unchangedReason(r: PrepareRow): string {
  const c = r.conflicts[0];
  if (c?.reason === "KEY_CASE_CONFLICT") return `it already has ${c.key} with different capitalisation, value ${c.existing_value}. Tag keys are case-sensitive, so a second key is not added`;
  if (c?.reason === "WOULD_OVERWRITE") return `it already has ${c.key} set to ${c.existing_value}; adding never overwrites`;
  if (c?.reason === "KEY_NOT_PRESENT") return `it has no ${c.key} tag to update`;
  if (c?.reason === "TAG_LIMIT_EXCEEDED") return "it already has the maximum number of tags";
  if (r.outcome === "POLICY_VIOLATION") return "the tagging policy doesn't allow that value";
  if (r.outcome === "INSPECTION_FAILED") return "its current tags couldn't be read";
  if (r.outcome === "ALREADY_MATCHING") return "it already has this tag and value";
  return r.outcome.toLowerCase().replace(/_/g, " ");
}

const STOPS = new Set(["DISABLED", "ACCOUNT_NOT_ALLOWED", "IDENTITY_UNVERIFIED", "operator_session_required", "network",
                       "ACCOUNT_MISMATCH", "EXECUTION_ROLE", "NOT_AUTHORIZED", "TOO_MANY"]);

export function onPrepare(flow: WriteFlow, out: PrepareOut): WriteStep {
  if (out.error) {
    if (STOPS.has(out.error)) return { flow: { ...flow, stage: "closed" }, say: out.speech ?? NOTHING };
    return { flow: { ...flow, stage: "need_tags", tag: undefined, overwrite: false },
             say: `${out.speech ?? NOTHING} ${ASK_TAGS}` };
  }
  if (out.status === "READY" && out.challenge) {
    const next: WriteFlow = { ...flow, stage: "confirm", challenge: out.challenge.id,
                              requiresAck: !!out.requires_overwrite_ack, mode: out.mode, plan: out, results: undefined };
    next.question = summary(next, out);
    return { flow: next, say: next.question };
  }
  const conflicts = (out.resources ?? []).some((r) => r.conflicts.length);
  if (out.status === "NO_CHANGES" && conflicts) {
    return { flow: { ...flow, stage: "conflict", plan: out },
             say: `${out.speech ?? ""} Say overwrite to replace the existing value, or say a different key and value.` };
  }
  return { flow: { ...flow, stage: "closed" }, say: out.speech ?? NOTHING };
}

/** The user's answer in any waiting stage. Only an explicit yes in "confirm" (plus the acknowledgement for
 *  overwrites) produces a confirm call. */
export function onWriteAnswer(flow: WriteFlow, text: string): WriteStep {
  if (CANCEL.test(text)) {
    return { flow: { ...flow, stage: "closed" }, say: `Okay, cancelled. ${NOTHING}`,
             ...(flow.challenge ? { cancel: flow.challenge } : {}) };
  }
  const tag = parseTagKeyValue(text);
  const newTag = (t: { key: string; value: string }, prefix: string): WriteStep => {
    const problem = tagProblem(t);
    if (problem) {
      return { flow: { ...flow, stage: "need_tags", tag: undefined, challenge: undefined }, say: `${problem} ${ASK_TAGS}`,
               ...(flow.challenge ? { cancel: flow.challenge } : {}) };
    }
    // A new key or value: the pending challenge is void; a new plan and a new confirmation are needed.
    return prepare({ ...flow, tag: t, partial: undefined, overwrite: false }, prefix, flow.challenge);
  };
  switch (flow.stage) {
    case "need_tags": {
      const got = parseTagAnswer(text, flow.partial);
      if (got && "tag" in got) return newTag(got.tag, "");
      if (got && "key" in got) {
        return { flow: { ...flow, partial: { key: got.key } },
                 say: `Okay, the key is ${got.key}. What value should ${got.key} have?` };
      }
      if (got && "value" in got) {
        return { flow: { ...flow, partial: { value: got.value } },
                 say: `Okay, the value is ${got.value}. Which tag key should have that value?` };
      }
      const missing = flow.partial?.key ? `What value should ${flow.partial.key} have?`
        : flow.partial?.value ? `Which tag key should have the value ${flow.partial.value}?` : ASK_TAGS;
      return { flow, say: `I didn't catch that. ${missing} Or say cancel.` };
    }
    case "conflict":
      if (tag) return newTag(tag, "Okay, a different tag. ");
      if (OVERWRITE_REQUEST.test(text)) return prepare({ ...flow, overwrite: true }, "Okay. Checking the overwrite.");
      return { flow, say: "Say overwrite, a different key and value, or cancel." };
    case "confirm": {
      if (tag) return newTag(tag, "Okay, I've changed the tag. ");
      const { answer } = parseConfirmation(text);
      if (answer === "yes") {
        if (flow.requiresAck) {
          return { flow: { ...flow, stage: "overwrite_ack", yes: text },
                   say: "This replaces existing values, as listed. To confirm the overwrite, say: I confirm the "
                     + "overwrite. Or say no." };
        }
        return { flow: { ...flow, stage: "executing" }, say: "Applying the tags now.",
                 call: { kind: "confirm", challenge: flow.challenge!, transcript: text } };
      }
      if (answer === "no") {
        return { flow: { ...flow, stage: "need_tags", tag: undefined, overwrite: false, challenge: undefined },
                 say: `Okay. ${NOTHING} What key and value should I use instead? Or say cancel.`,
                 cancel: flow.challenge };
      }
      return { flow, say: `I need a clear yes or no. ${flow.question ?? ""}` };
    }
    case "overwrite_ack": {
      if (OVERWRITE_ACK.has(normalizeUtterance(text))) {
        return { flow: { ...flow, stage: "executing" }, say: "Applying the tags now.",
                 call: { kind: "confirm", challenge: flow.challenge!, transcript: flow.yes!, overwrite: text } };
      }
      if (parseConfirmation(text).answer === "no") {
        return { flow: { ...flow, stage: "need_tags", tag: undefined, overwrite: false, challenge: undefined },
                 say: `Okay. ${NOTHING} What key and value should I use instead? Or say cancel.`,
                 cancel: flow.challenge };
      }
      return { flow, say: "To overwrite, say exactly: I confirm the overwrite. Or say no." };
    }
    default:
      return { flow, say: "" }; // busy (searching, preparing, executing) or finished: a repeated transcript does nothing
  }
}

export function onConfirm(flow: WriteFlow, out: ConfirmOut): WriteStep {
  if (out.status === "EXECUTED") {
    return { flow: { ...flow, stage: "done", challenge: undefined, results: out.results ?? [], outcome: out.outcome },
             say: out.speech ?? "" };
  }
  if (out.status === "CANCELLED") {
    return { flow: { ...flow, stage: "need_tags", tag: undefined, challenge: undefined },
             say: `${out.speech ?? NOTHING} ${ASK_TAGS}` };
  }
  if (out.error === "CHALLENGE_EXPIRED" || out.error === "PROPOSAL_CHANGED") {
    // Never retried with the old yes: a new plan, read back and confirmed again.
    return prepare(flow, `${out.speech ?? NOTHING} Here is the current plan again.`);
  }
  if (out.error === "NOT_CONFIRMED" || out.error === "OVERWRITE_NOT_ACKNOWLEDGED") {
    return { flow: { ...flow, stage: "confirm", yes: undefined }, say: `${out.speech ?? ""} ${flow.question ?? ""}` };
  }
  return { flow: { ...flow, stage: "closed", challenge: undefined }, say: out.speech ?? NOTHING };
}

/** True while the flow takes the user's next answer. */
export function awaitingWrite(flow: WriteFlow | null | undefined): flow is WriteFlow {
  return !!flow && ["need_tags", "confirm", "overwrite_ack", "conflict"].includes(flow.stage);
}

/** True while a call is running: transcripts arriving now (a repeated "yes") are ignored. */
export function busyWrite(flow: WriteFlow | null | undefined): boolean {
  return !!flow && ["searching", "preparing", "executing"].includes(flow.stage);
}
