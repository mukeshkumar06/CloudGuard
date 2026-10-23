// The group tagging conversation against the real fixture-mode server through the real Vite proxy: only the two
// read-only tools are ever called, and only after an explicit yes to the current summary is the preview run. Tool
// calls and their arguments are read from the real requests; only the browser speech APIs are faked (Playwright's Chromium has none).
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

const TAGGING = ["search_resources", "preview_tag_changes"];

test("group tagging (typed): search, ask, repeat, change, explicit yes, preview handed to the CLI", async ({ page }) => {
  const calls = await open(page);
  await expect(page.getByLabel("Preferred region")).toHaveValue("ap-south-1");
  let msg = await type(page, "Find all Lambdas with checkout in their names and tag them.");
  await expect(msg).toContainText("Which AWS region should I search"); // no region named: asked, never defaulted
  expect(calls).toEqual([]);
  msg = await type(page, "Mumbai");
  await expect(page.locator(".msg.assistant").last()).toContainText("What tag key and value", { timeout: 30_000 });
  msg = page.locator(".msg.assistant").last();
  await expect(msg).toContainText("Fixture mode, synthetic demo data, not your AWS account");
  await expect(msg).toContainText("1 Lambda function: checkout-api-handler");
  expect(calls).toEqual([{ name: "search_resources", args: { text: "checkout", match: "contains", services: ["lambda"],
                                                             region: "ap-south-1", limit: 100 } }]);

  msg = await type(page, "Key is cost-owner, value is mukesh.");
  for (const part of ["Service: Lambda functions", "Region: Asia Pacific (Mumbai), ap-south-1",
                      "Name filter: names containing checkout", "Matched: 1 Lambda function", "Tag key: cost-owner",
                      "Tag value: mukesh"]) await expect(msg).toContainText(part);
  const yesButtons = page.getByRole("button", { name: "Yes, prepare the proposal" });
  await expect(yesButtons).toHaveCount(1);

  msg = await type(page, "sounds good");
  await expect(msg).toContainText("I need a clear yes or no");
  msg = await type(page, "No, key is dev, value is mukesh.");
  await expect(msg).toContainText("Tag key: dev");
  // Only the new summary has a Yes: changing the tag invalidated the earlier one.
  await expect(yesButtons).toHaveCount(1);
  await expect(msg.getByRole("button", { name: "Yes, prepare the proposal" })).toBeVisible();
  expect(calls.map((c) => c.name)).toEqual(["search_resources"]);

  await msg.getByRole("button", { name: "Yes, prepare the proposal" }).click();
  const ready = page.getByLabel("Proposal request");
  await expect(ready).toContainText('"name_filter"', { timeout: 30_000 });
  await expect(ready).toContainText('"dev": "mukesh"');
  await expect(ready).toContainText("proposal approve");
  await expect(page.locator(".msg.assistant").last()).toContainText("Nothing has been applied");
  expect(calls.at(-1)).toEqual({ name: "preview_tag_changes", args: { tags: { dev: "mukesh" }, service: "lambda",
    name_match: "contains", name_text: "checkout", region: "ap-south-1" } });

  await type(page, "yes, approve and execute it");
  await page.waitForTimeout(300);
  expect(calls.every((c) => TAGGING.includes(c.name))).toBe(true);
  expect(calls.filter((c) => c.name === "preview_tag_changes")).toHaveLength(1);
});

test("group tagging (voice): an unclear answer or a new tag never confirms", async ({ page }) => {
  const calls = await open(page);
  const reply = page.getByLabel("CloudGuard reply");
  await speak(page, "Find all Lambdas with checkout in their names and tag them.");
  await expect(reply).toContainText('I heard: "Find all Lambdas with checkout in their names and tag them."', { timeout: 30_000 }); // read back before any search
  await speak(page, "yes");
  await speak(page, "Mumbai"); // the request named no region: asked for after the read-back
  await expect(reply).toContainText("What tag key and value", { timeout: 30_000 });
  await speak(page, "Key is cost-owner, value is mukesh.");
  await expect(reply).toContainText("Tag value: mukesh");
  await speak(page, "that's fine");
  await expect(reply).toContainText("I need a clear yes or no");
  await speak(page, "no");
  await expect(reply).toContainText("Nothing was written");
  await speak(page, "key is team, value is platform");
  await expect(reply).toContainText("Tag key: team");
  expect(calls.map((c) => c.name)).toEqual(["search_resources"]);
  await speak(page, "yes");
  await expect(reply).toContainText("can't approve or apply tags", { timeout: 30_000 });
  expect(calls.map((c) => c.name)).toEqual(TAGGING);
  expect(calls[1].args).toMatchObject({ tags: { team: "platform" } });
});
