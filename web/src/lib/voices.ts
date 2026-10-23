// Choosing a speechSynthesis voice for a preference. Pure functions over the voices the browser reports; no voice
// is assumed to exist. The Web Speech API has no gender field, so "female" and "male" are guessed from well-known
// voice names (Windows, macOS, Chrome, Edge); a voice whose name gives no hint is never claimed as either.

export type VoicePreference = "female" | "male" | "system";

export interface VoiceLike {
  name: string;
  lang: string;
  default?: boolean;
  localService?: boolean;
}

export interface VoiceChoice<V extends VoiceLike = VoiceLike> {
  voice: V | null; // null: let the browser use its default voice
  // How the choice was made: the preferred kind, any English voice instead, or the browser default.
  match: "preferred" | "english-fallback" | "browser-default";
}

const FEMALE = /\b(female|woman|zira|heera|neerja|swara|kalpana|veena|lekha|aditi|raveena|samantha|victoria|karen|moira|tessa|fiona|susan|hazel|catherine|aria|jenny|sonia|libby|natasha|clara|emma|michelle|ana|joanna|kendra|kimberly|salli|ivy|serena|allison|ava|nicky|kathy|vicki|princess)\b/i;
const MALE = /\b(male|man|david|mark|ravi|prabhat|hemant|madhur|rishi|daniel|alex|fred|george|guy|ryan|aaron|thomas|james|william|eric|christopher|roger|steffan|brian|matthew|joey|justin|russell|oliver|arthur|gordon|lee|tom|bruce|ralph|albert)\b/i;

export function voiceGender(v: VoiceLike): "female" | "male" | null {
  // "Female" contains "male", so check explicit female words first; names that match both are ambiguous.
  const f = FEMALE.test(v.name);
  const m = MALE.test(v.name.replace(/female/gi, ""));
  if (f && !m) return "female";
  if (m && !f) return "male";
  return null;
}

const isEnglish = (v: VoiceLike) => /^en([-_]|$)/i.test(v.lang);
const isIndianEnglish = (v: VoiceLike) => /^en[-_]in$/i.test(v.lang);

// Neural voices sound like a person; the older desktop voices (Microsoft Zira, David) sound robotic and can stutter.
// Edge lists "Microsoft … Online (Natural)" neural voices. Chrome's streamed "Google …" voices are not preferred:
// Chrome cuts them off partway through long answers.
const isNatural = (v: VoiceLike) => /\b(natural|neural)\b/i.test(v.name);

/** Lower is better: a natural (neural) voice, then Indian English, then the browser's default English voice, then
 *  on-device, then other English. */
function rank(v: VoiceLike): number {
  return (isNatural(v) ? 0 : 8) + (isIndianEnglish(v) ? 0 : 4) + (v.default ? 0 : 2) + (v.localService === false && !isNatural(v) ? 1 : 0);
}

function best<V extends VoiceLike>(vs: V[]): V | null {
  return vs.length ? [...vs].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name))[0] : null;
}

export function chooseVoice<V extends VoiceLike>(voices: readonly V[], pref: VoicePreference): VoiceChoice<V> {
  if (pref === "system") return { voice: null, match: "browser-default" };
  const english = voices.filter(isEnglish);
  const preferred = best(english.filter((v) => voiceGender(v) === pref));
  if (preferred) return { voice: preferred, match: "preferred" };
  const fallback = best(english);
  return fallback ? { voice: fallback, match: "english-fallback" } : { voice: null, match: "browser-default" };
}
