// Region names to AWS region codes, for "Show me my resources in Mumbai". Deterministic; no model.
//
// resolveRegion finds a region in free text: an AWS region code ("ap-south-1"), a city or area name ("Mumbai",
// "N. Virginia"), or a request for every region ("all regions"). A name that fits more than one region ("US East",
// "India") is ambiguous and is asked about, never guessed. The server still checks that the region is enabled.

// The regions a "which region?" question offers first, in this order (all are entries of REGIONS below). Any other
// region CloudGuard knows can still be named; the server checks that it is enabled for the account.
export const SUGGESTED_REGIONS = ["us-east-1", "us-west-2", "ap-south-1", "ap-southeast-1", "eu-west-1"] as const;

export const REGION_QUESTION = "Which AWS region should I search? For example US East (N. Virginia), US West (Oregon), "
  + "Asia Pacific (Mumbai), Asia Pacific (Singapore) or Europe (Ireland), or say all regions.";

export type RegionChoice =
  | { kind: "region"; code: string; label: string }
  | { kind: "all" }
  | { kind: "ambiguous"; candidates: string[] }
  | { kind: "none" };

// [code, display label, names people say for it]
const REGIONS: [string, string, string[]][] = [
  ["us-east-1", "US East (N. Virginia)", ["n virginia", "north virginia", "northern virginia", "virginia"]],
  ["us-east-2", "US East (Ohio)", ["ohio"]],
  ["us-west-1", "US West (N. California)", ["n california", "north california", "northern california", "california"]],
  ["us-west-2", "US West (Oregon)", ["oregon"]],
  ["ca-central-1", "Canada (Central)", ["canada", "montreal"]],
  ["sa-east-1", "South America (São Paulo)", ["sao paulo", "são paulo", "brazil"]],
  ["eu-west-1", "Europe (Ireland)", ["ireland", "dublin"]],
  ["eu-west-2", "Europe (London)", ["london"]],
  ["eu-west-3", "Europe (Paris)", ["paris"]],
  ["eu-central-1", "Europe (Frankfurt)", ["frankfurt"]],
  ["eu-north-1", "Europe (Stockholm)", ["stockholm", "sweden"]],
  ["ap-south-1", "Asia Pacific (Mumbai)", ["mumbai", "bombay"]],
  ["ap-south-2", "Asia Pacific (Hyderabad)", ["hyderabad"]],
  ["ap-southeast-1", "Asia Pacific (Singapore)", ["singapore"]],
  ["ap-southeast-2", "Asia Pacific (Sydney)", ["sydney"]],
  ["ap-northeast-1", "Asia Pacific (Tokyo)", ["tokyo"]],
  ["ap-northeast-2", "Asia Pacific (Seoul)", ["seoul"]],
  ["ap-northeast-3", "Asia Pacific (Osaka)", ["osaka"]],
  ["ap-east-1", "Asia Pacific (Hong Kong)", ["hong kong"]],
  ["me-central-1", "Middle East (UAE)", ["uae", "dubai"]],
];

// Names that fit more than one region.
const AMBIGUOUS: [string, string[]][] = [
  ["us east", ["us-east-1", "us-east-2"]],
  ["us west", ["us-west-1", "us-west-2"]],
  ["india", ["ap-south-1", "ap-south-2"]],
  ["japan", ["ap-northeast-1", "ap-northeast-3"]],
  ["europe", ["eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1", "eu-north-1"]],
  ["america", ["us-east-1", "us-east-2", "us-west-1", "us-west-2"]],
];

const ALL = /\b(all|every|each|any)( (the|my|aws|of the|enabled|supported))* regions?\b|\b(everywhere|all over|globally|global|worldwide|across regions)\b/;
// A bare answer to "which region?": "All.", "all of them", "everything", "both".
const ALL_ANSWER = /^ (all|all of them|all of it|everything|every one|everyone|both|the whole account|whole account) $/;
const CODE = /\b([a-z]{2}(?:-gov)?-(?:north|south|east|west|central|northeast|northwest|southeast|southwest)-\d{1,2})\b/;

/** Every region CloudGuard can name, for the simulator's region selector. */
export const REGION_OPTIONS: { code: string; label: string }[] = REGIONS.map(([code, label]) => ({ code, label }));

export function regionLabel(code: string): string {
  return REGIONS.find(([c]) => c === code)?.[1] ?? code;
}

function normalize(text: string): string {
  return ` ${text.toLowerCase().replace(/[.,!?;:()"']/g, " ").replace(/\s+/g, " ").trim()} `;
}

/** The choices a region question offers: the preferred region first (when it is one CloudGuard knows), then the
 *  suggested regions. Display only; nothing here selects a region. */
export function regionChoices(preferred?: string): { code: string; label: string }[] {
  const codes = [...new Set([...(preferred && REGIONS.some(([c]) => c === preferred) ? [preferred] : []),
                             ...SUGGESTED_REGIONS])];
  return codes.map((code) => ({ code, label: regionLabel(code) }));
}

/** "Which AWS region should I search for Lambda functions? ..." The choices are spoken in full. */
export function regionQuestion(what?: string, opts: { allRegions?: boolean } = {}): string {
  const names = SUGGESTED_REGIONS.map(regionLabel);
  const list = `${names.slice(0, -1).join(", ")} or ${names[names.length - 1]}`;
  return `Which AWS region should I search${what ? ` for ${what}` : ""}? For example ${list}`
    + `${opts.allRegions === false ? "." : ", or say all regions."}`;
}

/** The answer to a region question that offered SUGGESTED_REGIONS. As resolveRegion, except that a name fitting
 *  several regions picks the one offered choice whose label starts with it ("US East" -> US East (N. Virginia)).
 *  The result is always read back with its full label, so the choice is never silent. */
export function resolveRegionAnswer(text: string): RegionChoice {
  const choice = resolveRegion(text);
  if (choice.kind !== "ambiguous") return choice;
  const t = normalize(text).trim();
  const offered = SUGGESTED_REGIONS.filter((c) => choice.candidates.includes(c)
    && normalize(regionLabel(c).split(" (")[0]).trim() === t);
  return offered.length === 1 ? { kind: "region", code: offered[0], label: regionLabel(offered[0]) } : choice;
}

export function resolveRegion(text: string): RegionChoice {
  const raw = text.toLowerCase();
  const code = raw.match(CODE)?.[1];
  if (code) return { kind: "region", code, label: regionLabel(code) };
  const t = normalize(text);
  if (ALL.test(t) || ALL_ANSWER.test(t)) return { kind: "all" };
  const found = new Set<string>();
  for (const [c, , names] of REGIONS) if (names.some((n) => t.includes(` ${n} `))) found.add(c);
  if (found.size === 1) {
    const [c] = found;
    return { kind: "region", code: c, label: regionLabel(c) };
  }
  if (found.size > 1) return { kind: "ambiguous", candidates: [...found] };
  for (const [name, candidates] of AMBIGUOUS) if (t.includes(` ${name} `)) return { kind: "ambiguous", candidates };
  return { kind: "none" };
}

// "... in Atlantis": a place named at the end of a question that is not a region CloudGuard knows. Words that
// commonly follow "in" without naming a place are not a region request.
const PLACE = /\b(?:in|from|at)\s+(?:the\s+)?([a-z][a-z .'-]{2,40}?)(?:\s+regions?)?\s*[?.!]*$/i;
const NOT_A_PLACE = /^(my|our|your|the|this|that|these|those|it|them|there|here|aws|amazon|total|use|stock|general|detail|details|order|production|prod|dev|development|staging|test|account|cloud|all|every|each|any)\b/i;

/** A region the question names but that isn't known ("in Atlantis"), so it is asked about, never treated as all
 *  regions. Undefined when the question names no place at all. */
export function unknownPlace(text: string): string | undefined {
  const m = text.match(PLACE);
  if (!m || NOT_A_PLACE.test(m[1].trim())) return undefined;
  return resolveRegion(m[1]).kind === "none" ? m[1].trim() : undefined;
}

/** The follow-up when a reply names no usable region. Short, so it can be spoken. */
export function regionRetry(choice: RegionChoice): string {
  if (choice.kind === "ambiguous") {
    return `That could be ${choice.candidates.map(regionLabel).join(" or ")}. Which one, or all regions?`;
  }
  return "I didn't recognise that region. Say a city such as Mumbai, a region code such as ap-south-1, or all regions.";
}
