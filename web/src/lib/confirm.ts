// Parsing a spoken answer to "Shall I continue?". Deterministic; no model.
//
// The answer is read phrase by phrase from the start: confirmation phrases ("yes", "go ahead"), refusal phrases
// ("no", "not now") and fillers ("please", "thanks") may repeat in any order ("yes yes", "okay, go ahead please").
// Then:
//   - any refusal phrase in that opening            -> "no" (a refusal always wins; nothing runs)
//   - only confirmations, and nothing after them    -> "yes"
//   - confirmations followed by a refusal ("yes, but don't run it")  -> "no"
//   - confirmations followed by anything else ("yes, but check this month instead") -> "ambiguous"
//   - no opening phrase at all                      -> "ambiguous"
// A refusal followed by more words ("no, but show me the account") returns that remainder as `rest`, so the caller
// can treat it as a new question AFTER cancelling the pending call. "yes" is only ever returned for an answer made
// of nothing but confirmations and fillers.

export type ConfirmationAnswer = "yes" | "no" | "ambiguous";

export interface ParsedConfirmation {
  answer: ConfirmationAnswer;
  rest?: string; // only with "no": the words after the refusal, for a new question
}

const YES_PHRASES = [
  "yes", "yeah", "yep", "yup", "yea", "ya", "sure", "certainly", "absolutely", "definitely", "of course", "ok",
  "alright", "all right", "go ahead", "go for it", "continue", "proceed", "do it", "do that", "run it", "confirm",
  // "sounds good" and "that's fine" are deliberately absent: agreement in passing is not an explicit approval.
  "confirmed", "i confirm", "please do", "that's right", "correct", "affirmative",
];
const NO_PHRASES = [
  "no", "nope", "nah", "no thanks", "no thank you", "cancel", "cancel that", "cancel it", "stop", "don't", "do not",
  "don't do it", "do not do it", "don't run it", "do not run it", "not now", "not yet", "never mind", "abort",
  "negative", "wait", "hold on",
];
// Hesitations ("um, yes") are fillers: they neither confirm nor refuse.
const FILLERS = ["please", "thanks", "thank you", "then", "now", "cloudguard", "that", "it", "um", "uh", "er", "hmm", "oh"];
const CONNECTORS = new Set(["but", "and", "then", "so", "actually", "instead", "rather", "just", "also", "well"]);
// A refusal anywhere after the confirmations turns the whole answer into a refusal.
const LATER_REFUSAL = /\b(no|not|don't|do not|never|cancel|stop|wait|hold on|abort)\b/;

/** Lowercase, unify apostrophes and recognizer spellings, drop punctuation, collapse spaces. */
export function normalizeUtterance(text: string): string {
  return text
    .toLowerCase()
    .replace(/[‘’ʼ`]/g, "'")
    .replace(/\bo\s*\.\s*k\s*\.?(?=\s|$)/g, "ok ")
    .replace(/[-_/]+/g, " ")
    .replace(/[.,!?;:"()[\]{}…]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .split(" ")
    .map(canonicalWord)
    .join(" ");
}

// Variants speech recognizers produce for the same word.
function canonicalWord(w: string): string {
  if (/^ye+s+$/.test(w) || w === "yess" || w === "yas") return "yes";
  if (/^no+$/.test(w)) return "no";
  if (w === "okay" || w === "okey" || w === "k" || w === "kay") return "ok";
  if (w === "dont") return "don't";
  if (w === "thats") return "that's";
  if (w === "nevermind") return "never mind";
  return w;
}

const PHRASES: { words: string[]; kind: "yes" | "no" | "filler" }[] = [
  ...YES_PHRASES.map((p) => ({ words: p.split(" "), kind: "yes" as const })),
  ...NO_PHRASES.map((p) => ({ words: p.split(" "), kind: "no" as const })),
  ...FILLERS.map((p) => ({ words: p.split(" "), kind: "filler" as const })),
].sort((a, b) => b.words.length - a.words.length); // longest first: "no thanks" before "no"

function phraseAt(words: string[], i: number) {
  return PHRASES.find((p) => p.words.every((w, k) => words[i + k] === w));
}

export function parseConfirmation(text: string): ParsedConfirmation {
  const words = normalizeUtterance(text).split(" ").filter(Boolean);
  let i = 0;
  let yes = 0;
  let no = 0;
  while (i < words.length) {
    const p = phraseAt(words, i);
    if (!p) break;
    if (p.kind === "yes") yes++;
    if (p.kind === "no") no++;
    i += p.words.length;
  }
  let restWords = words.slice(i);
  while (restWords.length && CONNECTORS.has(restWords[0])) restWords = restWords.slice(1);
  const rest = restWords.join(" ");
  if (no > 0) return rest && yes === 0 ? { answer: "no", rest } : { answer: "no" };
  if (yes === 0) return { answer: "ambiguous" };
  if (!rest) return { answer: "yes" };
  return LATER_REFUSAL.test(rest) ? { answer: "no" } : { answer: "ambiguous" };
}

/** "yes", "no", or null when the answer is not a clear yes or no. */
export function confirmationOf(text: string): "yes" | "no" | null {
  const { answer } = parseConfirmation(text);
  return answer === "ambiguous" ? null : answer;
}
