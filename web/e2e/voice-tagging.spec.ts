// Personal voice tagging against the real fixture-mode server (simulated account, no AWS) through the real Vite
// proxy. The operator signs in with a form post (HttpOnly cookie); only spoken requests reach /operator writes,
// only after an explicit yes. Only the browser speech APIs are faked (Playwright's Chromium has none).
import { expect, test, type Page } from "@playwright/test";

const ALEXA = process.env.CG_E2E_ALEXA_TOKEN!;
const VOICE = process.env.CG_E2E_VOICE_TOKEN!;

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

async function open(page: Page, signIn = true) {
  const calls: { name: string; args: Record<string, unknown> }[] = [];
  page.on("request", (r) => {
    if (r.method() !== "POST" || new URL(r.url()).pathname !== "/mcp") return;
    const body = JSON.parse(r.postData() ?? "{}");
    if (body.method === "tools/call") calls.push({ name: body.params.name, args: body.params.arguments ?? {} });
  });
  await page.addInitScript(installFakeSpeech);
  await page.goto("/");
  if (signIn) {
    // A plain form post: the server answers with an HttpOnly cookie and reloads the page.
    await page.getByLabel("Operator credential").fill(VOICE);
    await page.getByRole("button", { name: "Sign in as operator" }).click();
    await expect(page.getByLabel("Operator voice tagging")).toContainText("Operator signed in");
  }
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


test("voice tagging: sign in, speak, correct, confirm by voice, per-resource result", async ({ page }) => {
  const writes: string[] = [];
  page.on("request", (r) => { if (new URL(r.url()).pathname.startsWith("/operator/voice-tagging/")) writes.push(new URL(r.url()).pathname); });
  const calls = await open(page);
  expect(await page.evaluate(() => document.cookie)).not.toContain("cg_operator"); // HttpOnly: invisible to scripts
  const reply = page.getByLabel("CloudGuard reply");

  await speak(page, "Find all Lambda functions containing legacy in Mumbai and tag them.");
  await expect(reply).toContainText('I heard: "Find all Lambda functions containing legacy in Mumbai and tag them."', { timeout: 30_000 }); // read back before any search
  await speak(page, "yes");
  await expect(reply).toContainText("I found 1 Lambda function matching legacy in Asia Pacific (Mumbai): legacy-report-generator",
                                    { timeout: 30_000 });
  await expect(reply).toContainText("Fixture mode, simulated account");
  await speak(page, "Key is owner, value is mukesh.");
  await expect(reply).toContainText("Tag value: mukesh", { timeout: 30_000 });
  await speak(page, "No, key is team, value is mukesh.");
  await expect(reply).toContainText("Tag key: team", { timeout: 30_000 });
  await speak(page, "sounds good");
  await expect(reply).toContainText("I need a clear yes or no");
  expect(writes.filter((w) => w.endsWith("/confirm"))).toEqual([]);
  await speak(page, "yes");
  await expect(reply).toContainText("Tagged and verified 1: legacy-report-generator", { timeout: 30_000 });
  expect(writes.filter((w) => w.endsWith("/confirm"))).toHaveLength(1);
  expect(writes.filter((w) => w.endsWith("/cancel"))).toHaveLength(1); // the owner=mukesh plan was voided
  expect(calls.every((c) => c.name === "search_resources")).toBe(true); // MCP stayed read-only

  // Typed chat never writes, even with the operator signed in: it stays a read-only preview.
  const before = writes.length;
  await type(page, "Find all Lambdas with legacy in their names and tag them.");
  await type(page, "Mumbai"); // the region question comes first
  await expect(page.locator(".msg.assistant").last()).toContainText("What tag key and value", { timeout: 30_000 });
  await type(page, "Key is project, value is x.");
  await type(page, "yes");
  await expect(page.getByLabel("Proposal request")).toBeVisible({ timeout: 30_000 });
  expect(writes.length).toBe(before);
});

test("voice tagging: without an operator session, speech stays preview-only", async ({ page }) => {
  const writes: string[] = [];
  page.on("request", (r) => { if (new URL(r.url()).pathname.startsWith("/operator/voice-tagging/")) writes.push(r.url()); });
  await open(page, false);
  await expect(page.getByLabel("Operator credential")).toBeVisible();
  const reply = page.getByLabel("CloudGuard reply");
  await speak(page, "Find all Lambdas with legacy in their names and tag them.");
  await expect(reply).toContainText('I heard: "Find all Lambdas with legacy in their names and tag them."', { timeout: 30_000 }); // read back before any search
  await speak(page, "yes");
  await speak(page, "Mumbai"); // the request named no region: asked for after the read-back
  await expect(reply).toContainText("What tag key and value", { timeout: 30_000 });
  await speak(page, "Key is project, value is x.");
  await speak(page, "yes");
  await expect(reply).toContainText("can't approve or apply tags", { timeout: 30_000 });
  expect(writes).toEqual([]);
});

test("voice tagging: 'Tag all the Lambdas which have …', key and value in separate turns, preview counts, no write", async ({ page }) => {
  const writes: string[] = [];
  page.on("request", (r) => { if (new URL(r.url()).pathname.startsWith("/operator/voice-tagging/")) writes.push(new URL(r.url()).pathname); });
  await open(page);
  const reply = page.getByLabel("CloudGuard reply");
  await speak(page, "Tag all the Lambdas which have legacy.");
  await expect(reply).toContainText('I heard: "Tag all the Lambdas which have legacy."', { timeout: 30_000 });
  await speak(page, "yes");
  await speak(page, "Mumbai"); // the request named no region: asked for after the read-back
  await expect(reply).toContainText("I found 1 Lambda function matching legacy", { timeout: 30_000 });
  await expect(reply).toContainText("What tag key and value would you like to apply?");
  await speak(page, "key is owner");
  await expect(reply).toContainText("What value should owner have?");
  expect(writes).toEqual([]); // nothing prepared with half a tag
  await speak(page, "Mukesh");
  await expect(reply).toContainText("Tag value: Mukesh", { timeout: 30_000 });
  await expect(page.getByLabel("Plan counts")).toContainText("Matched");
  await speak(page, "no");
  await expect(reply).toContainText("Nothing was written");
  expect(writes.filter((w) => w.endsWith("/confirm"))).toEqual([]);
});

test("voice tagging: the kill switch disables it at once", async ({ page }) => {
  await open(page);
  await page.getByRole("button", { name: "Disable voice tagging now" }).click();
  await expect(page.getByLabel("Operator voice tagging")).toContainText("kill switch is engaged");
  const r = await page.evaluate(async () => (await fetch("/operator/voice-tagging/prepare", {
    method: "POST", credentials: "same-origin", headers: { "content-type": "application/json", "x-cloudguard-voice": "1" },
    body: JSON.stringify({ service: "lambda", region: "ap-south-1", match: "contains", text: "legacy",
                           tags: { a: "b" }, channel: "voice" }) })).json());
  expect(r.error).toBe("DISABLED");
});
