// The personal voice-tagging API (/operator, see backend cloudguard/voice_tagging/web.py). The operator session
// is an HttpOnly cookie the browser sends by itself: this code never sees the operator credential or the session
// id, and stores nothing. Only the voice controller calls prepare/confirm; typed chat has no path here.

export interface OperatorSession {
  enabled: boolean;
  authenticated: boolean;
  operator: string | null;
  expires_at: string | null;
  mode: string | null;
  kill_switch: boolean;
}

export interface TagSpec {
  service: string;
  region: string;
  match: "contains" | "starts_with" | "ends_with" | "exact";
  text: string;
  tags: Record<string, string>;
  overwrite?: boolean;
}

export interface PrepareRow {
  name: string;
  region: string;
  outcome: string;
  current_value?: Record<string, string | null>; // the requested keys' current values (operator session only)
  changes: { key: string; action: string; old_value: string | null; new_value: string }[];
  conflicts: { key: string; existing_value: string | null; reason: string }[];
}

export interface PrepareOut {
  status?: string; // READY, NO_CHANGES, NOT_FOUND, NEEDS_CLARIFICATION, BLOCKED
  error?: string; // a refusal code
  speech?: string;
  mode?: string;
  account_ref?: string;
  resources?: PrepareRow[];
  overwrites?: { name: string; key: string; old_value: string | null; new_value: string }[];
  requires_overwrite_ack?: boolean;
  challenge?: { id: string; expires_at: string; plan_hash: string } | null;
}

export interface ConfirmOut {
  status?: string; // EXECUTED, CANCELLED, REFUSED
  error?: string;
  speech?: string;
  outcome?: string;
  replayed?: boolean;
  results?: { name: string; category: "tagged" | "already_correct" | "failed" | "skipped"; status: string;
              error: string; revalidation: string | null; detail: string }[];
}

export interface OperatorApi {
  prepare(spec: TagSpec): Promise<PrepareOut>;
  confirm(challengeId: string, transcript: string, overwriteTranscript?: string): Promise<ConfirmOut>;
  cancel(challengeId: string): Promise<void>;
}

const HEADERS = { "content-type": "application/json", "x-cloudguard-voice": "1" };

async function post<T>(path: string, body: unknown, keepalive = false): Promise<T> {
  try {
    const r = await fetch(path, { method: "POST", credentials: "same-origin", headers: HEADERS,
                                  body: JSON.stringify(body), keepalive });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return { error: data.error ?? `http_${r.status}`,
               speech: data.speech ?? (r.status === 401 ? "Sign in as the operator first. Nothing was written."
                 : "That request was refused. Nothing was written.") } as T;
    }
    return data as T;
  } catch {
    return { error: "network", speech: "I couldn't reach the server. Nothing was written." } as T;
  }
}

/** The session status, or null when voice tagging is disabled on the server (404) or unreachable. */
export async function fetchOperatorSession(): Promise<OperatorSession | null> {
  try {
    const r = await fetch("/operator/session", { credentials: "same-origin", cache: "no-store" });
    if (!r.ok) return null;
    return (await r.json()) as OperatorSession;
  } catch {
    return null;
  }
}

export async function logoutOperator(): Promise<void> {
  await post("/operator/logout", {});
}

export async function engageKillSwitch(): Promise<void> {
  await post("/operator/voice-tagging/kill", {});
}

export const operatorApi: OperatorApi = {
  prepare: (spec) => post<PrepareOut>("/operator/voice-tagging/prepare", { ...spec, channel: "voice" }),
  confirm: (challengeId, transcript, overwriteTranscript) => post<ConfirmOut>("/operator/voice-tagging/confirm", {
    challenge_id: challengeId, transcript, channel: "voice",
    ...(overwriteTranscript !== undefined ? { overwrite_transcript: overwriteTranscript } : {}) }),
  cancel: async (challengeId) => { await post("/operator/voice-tagging/cancel", { challenge_id: challengeId }, true); },
};
