// Resources by name in spoken or typed questions: "Why is OrderProcessor untagged?", "Explain the tagging issue with
// my Payments API Lambda", "What tags should I add to the assets bucket?". Deterministic; no model.
//
// The text after the question words is the name, minus filler ("my", "the") and a resource-type word ("Lambda",
// "bucket", "table", "instance", "database"), which becomes the service. The server matches the name against its
// latest scan and asks when several resources match, so nothing here guesses a resource.

export const SERVICE_NOUN: Record<string, string> = {
  lambda: "Lambda function", dynamodb: "DynamoDB table", s3: "S3 bucket", ec2: "EC2 instance",
};

// Type words, longest first, with the service they imply.
const TYPE_WORDS: [RegExp, string][] = [
  [/\b(lambda functions?|lambdas?|functions?)\b/i, "lambda"],
  [/\b(dynamo ?db tables?|dynamo ?db|tables?|databases?|db)\b/i, "dynamodb"],
  [/\b(s3 buckets?|s3|buckets?)\b/i, "s3"],
  [/\b(ec2 instances?|ec2|instances?|servers?|vms?|virtual machines?)\b/i, "ec2"],
];

const NAME_PATTERNS: RegExp[] = [
  /\bwhy (?:is|are|was|does|do) (.+?) (?:untagged|un tagged|non-?compliant|not tagged|flagged|failing|failed|missing\b|lacking\b|without tags|have no tags)/i,
  /\b(?:explain|describe|tell me about)(?: the| a)?(?: (?:tagging|tag))?(?: (?:issues?|problems?|findings?))?(?: (?:with|for|on|of|in|about))? (.+?)[?.!]*$/i,
  /\bwhat'?s wrong with (.+?)[?.!]*$/i,
  /\b(?:tags?|tag values?|values?) (?:should|do|would|must) (?:i|we) (?:add|use|set|put)? ?(?:to|for|on) (.+?)[?.!]*$/i,
  /\b(?:recommend|suggest|propose)\w* (?:tags?|tag values?|values?) (?:for|to|on) (.+?)[?.!]*$/i,
];

// What is left when the question names no particular resource.
const GENERIC = /^(it|this|that|them|these|those|this one|that one|resource|resources|this resource|that resource|my resources|finding|this finding|that finding|the finding|issue|problem|tag|tags|tagging|everything|all|all of them|untagged|my|our|the|a|an|its|their|any|please|details|that please|it please)$/i;
const FILLER = /^(?:(?:my|our|the|a|an|this|that|its|their)\s+)+|\s+(?:please|now|again|resource|one)$/gi;

export interface NamedResource {
  name?: string; // what the user called it, cleaned of filler and type words; undefined if none was named
  service?: string; // from a type word, when one was said
}

/** The resource a question names, if any. A tagcomp- finding ID is handled by the caller. */
export function resourceIn(question: string): NamedResource {
  let raw: string | undefined;
  for (const p of NAME_PATTERNS) {
    raw = question.match(p)?.[1];
    if (raw) break;
  }
  const service = TYPE_WORDS.find(([w]) => w.test(raw ?? question))?.[1];
  if (!raw) return service ? { service } : {};
  let name = raw;
  for (const [w] of TYPE_WORDS) name = name.replace(new RegExp(w.source, "gi"), " ");
  name = name.replace(/\s+/g, " ").trim().replace(FILLER, "").replace(FILLER, "").trim()
    // "this finding for my", "the issue with": words about the question, not a name
    .replace(/^(?:(?:this|that|the)\s+)?(?:(?:tagging|tag)\s+)?(?:findings?|issues?|problems?)\b(?:\s+(?:for|with|on|of|about|in))?\s*/i, "")
    .replace(/(?:\s+(?:for|with|on|of|about|in|my|the|our))+$/i, "")
    .replace(FILLER, "").trim();
  if (!name || GENERIC.test(name) || name.length > 128) return service ? { service } : {};
  return service ? { name, service } : { name };
}

/** "Which Lambda function would you like me to explain?" */
export function whichResourcePrompt(action: "explain" | "recommend", service?: string): string {
  const noun = service ? SERVICE_NOUN[service] : "resource";
  return action === "explain" ? `Which ${noun} would you like me to explain?`
    : `Which ${noun} should I recommend tags for?`;
}

type Row = { finding_id?: string; display_name?: string | null; service?: string };
type Structured = {
  speech?: string; resources?: Row[]; display_name?: string | null; finding_id?: string; service?: string;
  compliance_status?: string; missing_tags?: string[]; invalid_tags?: ({ key: string } | string)[];
};

const plural = (n: number, word: string) => `${word}${n === 1 ? "" : "s"}`;

/** What to say for a tool result, and the one resource it is about (for follow-ups). The server's speech never
 *  contains resource names; this adds the safe display names the server chose to send (never withheld ones), and
 *  only facts from the same result: tag keys, counts and statuses. Nothing is invented. */
export function spokenAnswer(tool: string, args: Record<string, unknown>, sc: Structured | undefined):
    { speech?: string; resource?: { finding_id: string; name?: string; service?: string } } {
  const speech = typeof sc?.speech === "string" ? sc.speech : undefined;
  if (!sc || !speech) return { speech };
  const rows = Array.isArray(sc.resources) ? sc.resources : [];
  const named = rows.map((r) => r.display_name).filter((n): n is string => typeof n === "string" && n.length > 0);
  const withheld = rows.length - named.length;
  const one = rows.length === 1 && rows[0].finding_id
    ? { finding_id: rows[0].finding_id, name: rows[0].display_name ?? undefined, service: rows[0].service } : undefined;
  if (tool === "list_untagged_resources" || tool === "search_resources"
      || (tool === "get_resource_inventory" && args.services)) {
    if (!named.length) return { speech, resource: one };
    const more = withheld ? ` ${withheld} more ${withheld === 1 ? "has a name" : "have names"} I won't read out.` : "";
    return { speech: `${speech} ${named.length === 1 ? "It is" : "They are"}: ${sayNames(named, 8)}.${more}`, resource: one };
  }
  if ((tool === "explain_finding" || tool === "recommend_tags") && sc.finding_id) {
    const name = sc.display_name ?? undefined;
    const resource = { finding_id: sc.finding_id, name, service: sc.service };
    if (!name) return { speech, resource };
    if (tool === "recommend_tags") return { speech: speech.replace(/^For the .+?\): /, `For ${name}: `), resource };
    const missing = sc.missing_tags ?? [];
    const invalid = (sc.invalid_tags ?? []).map((i) => (typeof i === "string" ? i : i.key));
    if (sc.compliance_status === "COMPLIANT") return { speech: `${name} has all its mandatory tags.`, resource };
    if (sc.compliance_status !== "NON_COMPLIANT" || (!missing.length && !invalid.length)) {
      return { speech: `About ${name}: ${speech}`, resource };
    }
    const parts: string[] = [];
    if (missing.length) parts.push(`${name} is missing the required ${sayNames(missing, 10)} ${plural(missing.length, "tag")}.`);
    if (invalid.length) {
      parts.push(`${missing.length ? "Its" : `${name}'s`} ${sayNames(invalid, 10)} ${plural(invalid.length, "tag")} `
        + `${invalid.length === 1 ? "has" : "have"} an invalid value.`);
    }
    return { speech: parts.join(" "), resource };
  }
  return { speech, resource: one };
}

/** A spoken list of names: "a, b and c", or "a, b, c and 4 more". */
export function sayNames(names: string[], max = 5): string {
  const shown = names.slice(0, max);
  const rest = names.length - shown.length;
  if (rest > 0) return `${shown.join(", ")} and ${rest} more`;
  return shown.length > 1 ? `${shown.slice(0, -1).join(", ")} and ${shown[shown.length - 1]}` : shown[0] ?? "";
}
