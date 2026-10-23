// Conversation-level answer cache: reuse a recent successful tool result instead of calling the server (and AWS)
// again for the same tool and arguments.
//
// In memory only: a plain Map inside this object. No browser storage of any kind is used, and nothing outlives the
// page. Entries hold the server's projected result (what the page already shows),
// never a token or header.
//
// Isolation: every entry belongs to a scope that the app sets on each successful connect (endpoint + a per-connection
// number), so two connections never share entries, and the app clears the cache on disconnect. If the AWS account
// reported by get_aws_account_identity changes within a connection, everything else is dropped too.

export const CACHE_TTL_MS: Readonly<Record<string, number>> = {
  get_aws_account_identity: 5 * 60_000,
  get_resource_inventory: 60_000, // the server reuses a scan for 60 s too
  scan_tag_compliance: 60_000,
  list_untagged_resources: 60_000,
  explain_finding: 5 * 60_000,
  recommend_tags: 5 * 60_000,
  get_monthly_aws_bill: 5 * 60_000,
  get_cost_breakdown: 5 * 60_000,
  compare_monthly_costs: 5 * 60_000,
  investigate_cost_increase: 5 * 60_000,
  get_cost_anomalies: 2 * 60_000,
  get_current_month_spending: 2 * 60_000,
};

export interface CachedAnswer<R = unknown> {
  tool: string;
  args: Record<string, unknown>;
  result: R;
  speech: string;
  storedAt: number;
  expiresAt: number;
  execId?: number; // the Results panel entry this answer came from
}

/** JSON with object keys sorted at every level, so {a,b} and {b,a} give the same key. */
export function stableArgs(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableArgs).join(",")}]`;
  if (value && typeof value === "object") {
    const o = value as Record<string, unknown>;
    return `{${Object.keys(o).filter((k) => o[k] !== undefined).sort()
      .map((k) => `${JSON.stringify(k)}:${stableArgs(o[k])}`).join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

/** Arguments compared as the server would see them: strings trimmed, empty strings dropped. */
export function normalizeArgs(args: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(args)) {
    const nv = typeof v === "string" ? v.trim() : v;
    if (nv === undefined || nv === "") continue;
    out[k] = nv;
  }
  return out;
}

interface StructuredLike {
  outcome?: unknown;
  speech?: unknown;
  coverage_complete?: unknown;
  account_id?: unknown;
}

/** Only complete, successful answers with a spoken reply are cached. */
export function isCacheable(tool: string, result: { isError?: boolean; structuredContent?: unknown } | undefined): boolean {
  if (!(tool in CACHE_TTL_MS) || !result || result.isError) return false;
  const sc = result.structuredContent as StructuredLike | undefined;
  if (!sc || typeof sc.speech !== "string" || !sc.speech) return false;
  if (sc.outcome !== "answered" && sc.outcome !== "findings") return false; // never "unavailable" or missing
  if (sc.coverage_complete === false) return false; // a partial scan is not reused
  return true;
}

export class AnswerCache<R extends { isError?: boolean; structuredContent?: unknown } = { isError?: boolean; structuredContent?: unknown }> {
  private entries = new Map<string, CachedAnswer<R>>();
  private scope: string | null = null;
  private account: string | null = null;

  constructor(private now: () => number = () => Date.now()) {}

  /** Start a new connection's cache. Anything from another scope is dropped. */
  setScope(scope: string | null): void {
    if (scope === this.scope) return;
    this.clear();
    this.scope = scope;
  }

  clear(): void {
    this.entries.clear();
    this.account = null;
  }

  get size(): number {
    return this.entries.size;
  }

  private key(tool: string, args: Record<string, unknown>): string | null {
    return this.scope === null ? null : `${this.scope}\u0000${tool}\u0000${stableArgs(normalizeArgs(args))}`;
  }

  get(tool: string, args: Record<string, unknown>): CachedAnswer<R> | null {
    const k = this.key(tool, args);
    if (k === null) return null;
    const e = this.entries.get(k);
    if (!e) return null;
    if (this.now() >= e.expiresAt) {
      this.entries.delete(k);
      return null;
    }
    return e;
  }

  /** Store a result if it is cacheable; returns whether it was stored. */
  put(tool: string, args: Record<string, unknown>, result: R, execId?: number): boolean {
    const k = this.key(tool, args);
    if (k === null || !isCacheable(tool, result)) return false;
    const sc = result.structuredContent as StructuredLike;
    if (tool === "get_aws_account_identity" && typeof sc.account_id === "string") {
      // A different AWS account behind the same connection: nothing cached so far can be trusted.
      if (this.account !== null && this.account !== sc.account_id) this.entries.clear();
      this.account = sc.account_id;
    }
    const at = this.now();
    this.entries.set(k, { tool, args: normalizeArgs(args), result, speech: sc.speech as string, storedAt: at,
                          expiresAt: at + CACHE_TTL_MS[tool], execId });
    return true;
  }

  /** Drop one entry (the Refresh action replaces it with a fresh result). */
  invalidate(tool: string, args: Record<string, unknown>): void {
    const k = this.key(tool, args);
    if (k !== null) this.entries.delete(k);
  }
}

/** "just now", "1 minute ago", "3 minutes ago". */
export function ageText(ms: number): string {
  const min = Math.floor(ms / 60_000);
  if (min < 1) return "just now";
  return min === 1 ? "1 minute ago" : `${min} minutes ago`;
}
