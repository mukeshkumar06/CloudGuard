// Conversation answer cache: the cache module, and the voice controller's use of it. No browser, no network.
import { describe, expect, it, vi } from "vitest";

import { AnswerCache, CACHE_TTL_MS, isCacheable, normalizeArgs, stableArgs } from "../src/lib/cache";
import { mapIntent } from "../src/lib/intent";
import {
  CACHED_INTRO, VoiceController, type CacheHit, type RecognitionLike, type SynthLike, type UtteranceLike,
  type VoiceDeps,
} from "../src/lib/voice";
import type { VoiceLike } from "../src/lib/voices";

type R = { isError?: boolean; structuredContent?: unknown };
const ok = (speech: string, extra: Record<string, unknown> = {}): R =>
  ({ isError: false, structuredContent: { outcome: "answered", speech, ...extra } });
const IDENTITY = (account: string) => ok(`account ending in ${account.slice(-4)}`, { account_id: account });

function cache() {
  let now = 1_000_000;
  const c = new AnswerCache<R>(() => now);
  c.setScope("/mcp#1");
  return { c, tick: (ms: number) => { now += ms; } };
}

describe("AnswerCache", () => {
  it("1. an identical request reuses the stored answer", () => {
    const { c } = cache();
    expect(c.get("get_monthly_aws_bill", { month: "last-month" })).toBeNull();
    expect(c.put("get_monthly_aws_bill", { month: "last-month" }, ok("Bill was 12 dollars."), 7)).toBe(true);
    expect(c.get("get_monthly_aws_bill", { month: "last-month" })).toMatchObject({ speech: "Bill was 12 dollars.", execId: 7 });
  });

  it("2. equivalent questions map to the same tool and arguments, so they share one entry", () => {
    const tools = ["get_aws_account_identity", "get_monthly_aws_bill"];
    const qs = ["Which AWS account am I using?", "What AWS account is CloudGuard connected to?",
      "Show me my AWS account ID.", "Who am I logged in as?"];
    const plans = qs.map((q) => mapIntent(q, tools));
    for (const p of plans) expect(p).toMatchObject({ kind: "call", tool: "get_aws_account_identity", args: {} });
    const { c } = cache();
    c.put("get_aws_account_identity", {}, IDENTITY("123456789012"));
    for (const p of plans) {
      if (p.kind !== "call") throw new Error("unexpected");
      expect(c.get(p.tool, p.args)?.speech).toBe("account ending in 9012");
    }
    expect(c.size).toBe(1);
  });

  it("argument order, surrounding spaces and empty values don't create separate entries", () => {
    expect(stableArgs({ b: 1, a: { d: 2, c: 3 } })).toBe(stableArgs({ a: { c: 3, d: 2 }, b: 1 }));
    expect(normalizeArgs({ month: " last-month ", cursor: "" })).toEqual({ month: "last-month" });
    const { c } = cache();
    c.put("get_cost_breakdown", { month: "2026-09", top: 5 }, ok("x"));
    expect(c.get("get_cost_breakdown", { top: 5, month: " 2026-09" })).not.toBeNull();
  });

  it("3. different tools or arguments are separate entries; an unrelated question gets nothing", () => {
    const { c } = cache();
    c.put("get_monthly_aws_bill", { month: "last-month" }, ok("September bill."));
    expect(c.get("get_monthly_aws_bill", { month: "2026-08" })).toBeNull();
    expect(c.get("get_monthly_aws_bill", {})).toBeNull();
    expect(c.get("get_cost_breakdown", { month: "last-month" })).toBeNull();
    c.put("explain_finding", { finding_id: "tagcomp-aaaaaaaaaaaaaaaa" }, ok("A."));
    expect(c.get("explain_finding", { finding_id: "tagcomp-bbbbbbbbbbbbbbbb" })).toBeNull();
  });

  it("4. entries expire after the tool's TTL", () => {
    const { c, tick } = cache();
    c.put("get_cost_anomalies", {}, ok("No anomalies."));
    c.put("get_monthly_aws_bill", {}, ok("Bill."));
    tick(CACHE_TTL_MS.get_cost_anomalies - 1);
    expect(c.get("get_cost_anomalies", {})).not.toBeNull();
    tick(1);
    expect(c.get("get_cost_anomalies", {})).toBeNull(); // 2 minutes
    expect(c.get("get_monthly_aws_bill", {})).not.toBeNull(); // 5 minutes
    tick(3 * 60_000);
    expect(c.get("get_monthly_aws_bill", {})).toBeNull();
  });

  it("TTLs are exactly as specified, for the existing tools only", () => {
    expect(CACHE_TTL_MS).toEqual({
      get_aws_account_identity: 300_000, get_resource_inventory: 60_000, scan_tag_compliance: 60_000,
      list_untagged_resources: 60_000, explain_finding: 300_000, recommend_tags: 300_000,
      get_monthly_aws_bill: 300_000, get_cost_breakdown: 300_000, compare_monthly_costs: 300_000,
      investigate_cost_increase: 300_000, get_cost_anomalies: 120_000, get_current_month_spending: 120_000,
    });
  });

  it.each([
    ["tool error", { isError: true, structuredContent: { outcome: "answered", speech: "x" } }],
    ["unavailable", { isError: false, structuredContent: { outcome: "unavailable", speech: "AWS was unreachable." } }],
    ["partial scan", ok("Some services failed.", { coverage_complete: false })],
    ["no speech", { isError: false, structuredContent: { outcome: "answered" } }],
    ["no outcome", { isError: false, structuredContent: { speech: "x" } }],
    ["no structured content", { isError: false }],
  ])("5. %s is not cached", (_label, result) => {
    const { c } = cache();
    expect(c.put("scan_tag_compliance", {}, result as R)).toBe(false);
    expect(c.get("scan_tag_compliance", {})).toBeNull();
  });

  it("5b. tools without a TTL are never cached; findings outcomes are", () => {
    expect(isCacheable("approve_fix", ok("x"))).toBe(false);
    expect(isCacheable("get_cost_anomalies", { isError: false, structuredContent: { outcome: "findings", speech: "2 anomalies." } })).toBe(true);
  });

  it("6. clearing on disconnect drops everything, and a cleared scope stores nothing", () => {
    const { c } = cache();
    c.put("get_resource_inventory", {}, ok("8 resources."));
    c.setScope(null);
    c.clear();
    expect(c.size).toBe(0);
    expect(c.put("get_resource_inventory", {}, ok("8 resources."))).toBe(false); // not connected
    expect(c.get("get_resource_inventory", {})).toBeNull();
  });

  it("7. a new endpoint/connection scope or a different AWS account clears the cache", () => {
    const { c } = cache();
    c.put("get_resource_inventory", {}, ok("8 resources."));
    c.setScope("/mcp#2"); // reconnect, or another endpoint
    expect(c.get("get_resource_inventory", {})).toBeNull();

    c.put("get_aws_account_identity", {}, IDENTITY("111111111111"));
    c.put("get_monthly_aws_bill", {}, ok("Bill for account 1."));
    c.put("get_aws_account_identity", {}, IDENTITY("111111111111")); // same account: kept
    expect(c.get("get_monthly_aws_bill", {})).not.toBeNull();
    c.put("get_aws_account_identity", {}, IDENTITY("222222222222")); // the account changed behind the connection
    expect(c.get("get_monthly_aws_bill", {})).toBeNull();
    expect(c.get("get_aws_account_identity", {})?.speech).toBe("account ending in 2222");
  });

  it("stores only the result it is given: no token or header fields exist on an entry", () => {
    const { c } = cache();
    c.put("get_monthly_aws_bill", {}, ok("Bill."));
    expect(Object.keys(c.get("get_monthly_aws_bill", {})!).sort())
      .toEqual(["args", "execId", "expiresAt", "result", "speech", "storedAt", "tool"]);
  });
});

// ---------- controller ----------

const HEERA: VoiceLike = { name: "Microsoft Heera - English (India)", lang: "en-IN" };

class Rec implements RecognitionLike {
  lang = ""; interimResults = false; continuous = true; maxAlternatives = 0;
  onresult: RecognitionLike["onresult"] = null;
  onerror: RecognitionLike["onerror"] = null;
  onend: RecognitionLike["onend"] = null;
  onstart: RecognitionLike["onstart"] = null;
  start() { this.onstart?.(); }
  stop() { this.onend?.(); }
  abort() { this.onerror?.({ error: "aborted" }); this.onend?.(); }
  hear(t: string) {
    this.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript: t }], { isFinal: true })] });
    this.onend?.();
  }
}

class Synth implements SynthLike {
  spoken: UtteranceLike[] = [];
  active: UtteranceLike | null = null;
  getVoices() { return [HEERA]; }
  speak(u: UtteranceLike) { this.active = u; this.spoken.push(u); }
  cancel() { const u = this.active; this.active = null; u?.onerror?.(); }
  finish() { const u = this.active; this.active = null; u?.onend?.(); }
}

/** The controller wired to a real AnswerCache the way App.tsx wires it: execute stores, lookup reads. */
function setup() {
  const store = cache();
  const recs: Rec[] = [];
  const synth = new Synth();
  const server = vi.fn(async (tool: string, _args: Record<string, unknown>) => ok(`Fresh answer from ${tool}.`));
  const execute = vi.fn<VoiceDeps["execute"]>(async (tool, args, opts) => {
    if (!opts?.refresh) {
      const hit = store.c.get(tool, args);
      if (hit) return { speech: hit.speech };
    }
    const r = await server(tool, args);
    store.c.put(tool, args, r);
    return { speech: (r.structuredContent as { speech: string }).speech };
  });
  const lookup = vi.fn((tool: string, args: Record<string, unknown>): CacheHit | null => {
    const hit = store.c.get(tool, args);
    return hit ? { speech: hit.speech, ageMs: 0 } : null;
  });
  const c = new VoiceController({
    createRecognition: () => { const r = new Rec(); recs.push(r); return r; },
    synth, createUtterance: (text) => ({ text, lang: "", onend: null, onerror: null }),
    tools: () => ["get_aws_account_identity", "get_monthly_aws_bill"], execute, lookup,
  }, "female", { autoListen: false });
  const flush = () => new Promise((r) => setTimeout(r, 0));
  const say = async (t: string) => { c.startListening(); recs[recs.length - 1].hear(t); await flush(); };
  return { c, recs, synth, server, execute, lookup, say, flush, store };
}

describe("voice controller with the cache", () => {
  it("2/9. a repeated or equivalent spoken question is answered from the cache with no call and no confirmation", async () => {
    const { c, synth, server, say } = setup();
    await say("Which AWS account am I using?");
    await say("yes");
    expect(server).toHaveBeenCalledTimes(1);
    for (const q of ["Which AWS account am I using?", "What AWS account is CloudGuard connected to?",
      "Show me my AWS account ID.", "Who am I logged in as?"]) {
      await say(q);
      expect(c.snapshot.pending).toBeNull(); // nothing to confirm: no tool runs
      expect(c.snapshot.lastCall).toEqual({ tool: "get_aws_account_identity", outcome: "cached" });
      expect(c.snapshot.cached).toMatchObject({ tool: "get_aws_account_identity", args: {} });
      expect(synth.spoken.at(-1)!.text).toBe(`${CACHED_INTRO("less than a minute ago")} Fresh answer from get_aws_account_identity.`);
    }
    expect(server).toHaveBeenCalledTimes(1);
  });

  it("9. a question answered through another path (typed) is reused by voice", async () => {
    const { server, say, store, c } = setup();
    store.c.put("get_monthly_aws_bill", { month: "last-month" }, ok("Typed answer."));
    await say("What was my AWS bill last month?");
    expect(server).not.toHaveBeenCalled();
    expect(c.snapshot.message).toContain("Typed answer.");
  });

  it("3. a different question is not answered from the cache", async () => {
    const { c, server, say } = setup();
    await say("Which AWS account am I using?");
    await say("yes");
    await say("What was my AWS bill last month?");
    expect(c.snapshot.cached).toBeNull();
    expect(c.snapshot.pending).toMatchObject({ tool: "get_monthly_aws_bill" });
    expect(server).toHaveBeenCalledTimes(1);
  });

  it("8. Refresh asks for confirmation, then bypasses the cache exactly once", async () => {
    const { c, server, execute, say, synth } = setup();
    await say("Which AWS account am I using?");
    await say("yes");
    await say("Which AWS account am I using?");
    c.refresh();
    expect(c.snapshot.pending).toMatchObject({ tool: "get_aws_account_identity", refresh: true });
    expect(synth.spoken.at(-1)!.text).toBe("I can fetch fresh data to check which AWS account CloudGuard is using. Shall I continue?");
    expect(server).toHaveBeenCalledTimes(1); // nothing yet
    c.refresh(); // a second click while pending does nothing
    await say("yes");
    expect(server).toHaveBeenCalledTimes(2);
    expect(execute).toHaveBeenLastCalledWith("get_aws_account_identity", {}, { refresh: true });
    expect(c.snapshot.cached).toBeNull();
    expect(c.snapshot.lastCall).toEqual({ tool: "get_aws_account_identity", outcome: "ok" });
  });

  it("refresh can be declined", async () => {
    const { c, server, say } = setup();
    await say("Which AWS account am I using?");
    await say("yes");
    await say("Which AWS account am I using?");
    c.refresh();
    await say("no");
    expect(server).toHaveBeenCalledTimes(1);
  });

  it("10. cached speech uses the selected voice", async () => {
    const { c, synth, say } = setup();
    await say("Which AWS account am I using?");
    await say("yes");
    c.setVoicePreference("female");
    await say("Who am I logged in as?");
    expect(synth.spoken.at(-1)).toMatchObject({ voice: HEERA, lang: "en-IN", volume: 1, rate: 1, pitch: 1 });
  });

  it("11. repeated recognition events make no duplicate server calls", async () => {
    const { recs, server, say, flush, c } = setup();
    await say("Which AWS account am I using?");
    c.startListening();
    const session = recs.at(-1)!;
    session.hear("yes");
    session.hear("yes"); // duplicate events from the same (ended) session
    await flush();
    await say("Which AWS account am I using?");
    await say("Which AWS account am I using?");
    expect(server).toHaveBeenCalledTimes(1);
  });

  it("6. after disconnect (reset) the controller forgets the reused answer", async () => {
    const { c, say } = setup();
    await say("Which AWS account am I using?");
    await say("yes");
    await say("Which AWS account am I using?");
    expect(c.snapshot.cached).not.toBeNull();
    c.reset();
    expect(c.snapshot.cached).toBeNull();
  });
});
