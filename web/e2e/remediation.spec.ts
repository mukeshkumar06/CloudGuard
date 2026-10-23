// QA audit fixes against the real fixture-mode server through the real Vite proxy: forced refresh reaches the
// server, typed cancellation of a region question, invalid and named months. Tool calls and their arguments are
// read from the real requests; only the browser speech APIs are faked (Playwright's Chromium has none).
import { expect, test, type Page } from "@playwright/test";

const ALEXA = process.env.CG_E2E_ALEXA_TOKEN!;

function installFakeSpeech() {
  const v = { recs: [] as { onresult: (e: unknown) => void; onend: () => void }[], spoken: [] as string[],
              active: null as null | { onend: (() => void) | null; onerror: (() => void) | null } };
  (window as unknown as { __voice: typeof v }).__voice = v;
  class FakeRecognition {
    lang = ""; interimResults = false; continuous = false; maxAlternatives = 1;
    onresult = null; onerror = null; onend: (() => void) | null = null; onstart: (() => void) | null = null;
    start() { v.recs.push(this as never); this.onstart?.(); }
    stop() { this.onend?.(); }
    abort() { this.onend?.(); }
  }
  class FakeUtterance { lang = ""; onend = null; onerror = null; constructor(public text: string) {} }
  const w = window as unknown as Record<string, unknown>;
  w.webkitSpeechRecognition = FakeRecognition;
  Object.defineProperty(window, "speechSynthesis", { value: {
    speak(u: FakeUtterance) { v.active = u; v.spoken.push(u.text); },
    cancel() { const u = v.active; v.active = null; u?.onerror?.(); },
  }, configurable: true });
  w.SpeechSynthesisUtterance = FakeUtterance;
}

async function open(page: Page) {
  const calls: { name: string; args: Record<string, unknown> }[] = [];
  page.on("request", (r) => {
    if (r.method() !== "POST" || new URL(r.url()).pathname !== "/mcp") return;
    const body = JSON.parse(r.postData() ?? "{}");
    if (body.method === "tools/call") calls.push({ name: body.params.name, args: body.params.arguments ?? {} });
  });
  await page.addInitScript(installFakeSpeech);
  await page.goto("/");
  await page.getByLabel("Alexa bearer token").fill(ALEXA);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.locator(".conn-pill")).toHaveText("● Connected");
  await page.getByLabel("Auto-listen").uncheck();
  return calls;
}

async function type(page: Page, q: string) {
  await page.getByPlaceholder("Ask about your AWS bill or resource tagging…").fill(q);
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  return page.locator(".msg.assistant").last();
}

async function speak(page: Page, text: string) {
  await page.getByRole("button", { name: "Start voice input" }).click();
  await expect(page.locator(".voice")).toHaveAttribute("data-status", "listening");
  await page.evaluate((t) => {
    const v = (window as unknown as { __voice: { recs: { onresult: (e: unknown) => void; onend: () => void }[] } }).__voice;
    const r = v.recs[v.recs.length - 1];
    r.onresult({ resultIndex: 0, results: [Object.assign([{ transcript: t }], { isFinal: true })] });
    r.onend();
  }, text);
}

const succeeded = (msg: ReturnType<Page["locator"]>) =>
  expect(msg.locator(".result-head .pill").first()).toContainText("Succeeded", { timeout: 30_000 });

test("refresh: typed and voice Refresh data send refresh: true; normal repeats reuse the cache", async ({ page }) => {
  // A complete inventory is cacheable (the fixture's compliance scan is partial, and partial scans never are).
  const calls = await open(page);
  const args = { region: "ap-south-1" };
  let msg = await type(page, "Show me my AWS resources in Mumbai.");
  await succeeded(msg);
  msg = await type(page, "How many resources do I have in ap-south-1?"); // same answer, from the browser cache
  await expect(msg.locator(".cache-note")).toContainText("Reused answer");
  expect(calls).toEqual([{ name: "get_resource_inventory", args }]);

  await msg.getByRole("button", { name: "Refresh data" }).click();
  await succeeded(msg);
  expect(calls.at(-1)).toEqual({ name: "get_resource_inventory", args: { ...args, refresh: true } });

  await speak(page, "Show me my AWS resources in Mumbai.");
  await expect(page.locator(".voice-call.cached")).toContainText("get_resource_inventory");
  const before = calls.length;
  await page.locator(".voice").getByRole("button", { name: "Refresh data" }).click();
  await expect(page.locator(".voice-call.ok")).toContainText("get_resource_inventory");
  expect(calls.length).toBe(before + 1);
  expect(calls.at(-1)).toEqual({ name: "get_resource_inventory", args: { ...args, refresh: true } });
});

test("cancel: typed cancel clears a pending region question; the next question runs normally", async ({ page }) => {
  const calls = await open(page);
  await type(page, "Show me my AWS resources in Atlantis."); // an unknown region: CloudGuard asks which
  let msg = await type(page, "Cancel");
  await expect(msg).toContainText("Okay, cancelled. Nothing was run.");
  msg = await type(page, "Mumbai"); // no longer an answer to anything
  await expect(msg).not.toContainText("Succeeded");
  expect(calls).toEqual([]);
  msg = await type(page, "Which AWS identity are you using?");
  await succeeded(msg);
  expect(calls).toEqual([{ name: "get_aws_account_identity", args: {} }]);
});

test("months: an invalid month runs nothing; named months reach the server explicitly", async ({ page }) => {
  const calls = await open(page);
  let msg = await type(page, "What did I pay in 2026-13?");
  await expect(msg).toContainText("2026-13 isn't a valid month");
  expect(calls).toEqual([]);

  msg = await type(page, "Compare my costs for August and September 2026");
  await succeeded(msg);
  expect(calls).toEqual([{ name: "compare_monthly_costs", args: { earlier_month: "2026-08", later_month: "2026-09" } }]);
});
