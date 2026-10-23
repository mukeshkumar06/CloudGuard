// Untagged resources by type and resources by name, against the real fixture-mode server through the real Vite
// proxy and the Alexa token (so names arrive only as the server's sanitized display_name). Tool calls and arguments
// are read from the real requests; only the browser speech APIs are faked.
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

async function type(page: Page, q: string) {
  await page.getByPlaceholder("Ask about your AWS bill or resource tagging…").fill(q);
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  return page.locator(".msg.assistant").last();
}

const lastSpoken = (page: Page) =>
  page.evaluate(() => (window as unknown as { __voice: { spoken: string[] } }).__voice.spoken.at(-1) ?? "");

test("voice: untagged Lambdas by name, then why, then what to add — all from tool results", async ({ page }) => {
  const calls = await open(page);
  await speak(page, "List all the Lambdas which are untagged.");
  expect(calls).toEqual([]); // which region? first
  await speak(page, "Mumbai");
  await expect(page.locator(".voice-call.ok")).toContainText("list_untagged_resources");
  expect(calls.at(-1)).toEqual({ name: "list_untagged_resources", args: { services: ["lambda"], region: "ap-south-1" } });
  expect(await lastSpoken(page)).toContain("1 Lambda function is missing mandatory tags. Showing 1 to 1. "
    + "It is: legacy-report-generator.");

  await speak(page, "Why is legacy-report-generator untagged?");
  await expect(page.locator(".voice-call.ok")).toContainText("explain_finding");
  expect(calls.at(-1)).toEqual({ name: "explain_finding", args: { resource_name: "legacy-report-generator" } });
  expect(await lastSpoken(page)).toBe("legacy-report-generator is missing the required Environment, Project, Owner "
    + "and CostCenter tags.");

  await speak(page, "What tags should I add?");
  await expect(page.locator(".voice-call.ok")).toContainText("recommend_tags");
  expect(calls.at(-1)?.name).toBe("recommend_tags");
  expect(calls.at(-1)?.args.resource).toMatch(/^tagcomp-[0-9a-f]{16}$/); // the same resource, by its finding ID
  expect(await lastSpoken(page)).toMatch(/^For legacy-report-generator: .* Nothing was changed\.$/);
});

test("voice: 'Which Lambda function?' offers names from the server, and the spoken name is explained", async ({ page }) => {
  const calls = await open(page);
  await speak(page, "Why is my Lambda function non-compliant?");
  await expect(page.locator(".voice-reply")).toContainText("Which Lambda function would you like me to explain?");
  await expect(page.locator(".voice-reply")).toContainText("legacy-report-generator");
  expect(calls.map((c) => c.name)).toEqual(["list_untagged_resources"]); // the names, read-only
  await speak(page, "legacy-report-generator");
  await expect(page.locator(".voice-call.ok")).toContainText("explain_finding");
  expect(calls.at(-1)).toEqual({ name: "explain_finding",
                                 args: { service: "lambda", resource_name: "legacy-report-generator" } });
});

test("typed: which region first, names listed, a name chip explains the right resource", async ({ page }) => {
  const calls = await open(page);
  let msg = await type(page, "Show my S3 buckets");
  await expect(msg).toContainText("Which AWS region should I search for S3 buckets?");
  expect(calls).toEqual([]); // nothing is discovered before the region is chosen
  msg = await type(page, "All regions");
  await expect(msg.locator(".answer")).toContainText("acme-finance-archive and acme-marketing-uploads", { timeout: 30_000 });
  expect(calls).toEqual([{ name: "get_resource_inventory", args: { services: ["s3"], region: "all" } }]);

  msg = await type(page, "Why is my Lambda function non-compliant?");
  await expect(msg).toContainText("Which Lambda function would you like me to explain?");
  await msg.getByRole("button", { name: "legacy-report-generator" }).click();
  msg = page.locator(".msg.assistant").last();
  await expect(msg.locator(".answer")).toContainText("legacy-report-generator is missing the required", { timeout: 30_000 });
  expect(calls.at(-1)).toEqual({ name: "explain_finding",
                                 args: { service: "lambda", resource_name: "legacy-report-generator" } });

  msg = await type(page, "Why is nightly-batch-thing untagged?");
  await expect(msg.locator(".result")).toContainText("no scanned resource has that name", { timeout: 30_000 });
});

test("search: names by filter across every region, and tagging requests change nothing", async ({ page }) => {
  const calls = await open(page);
  let msg = await type(page, "Find resources starting with acme in all regions");
  await expect(msg.locator(".answer")).toContainText("They are: acme-finance-archive and acme-marketing-uploads",
                                                      { timeout: 30_000 });
  expect(calls).toEqual([{ name: "search_resources", args: { text: "acme", match: "starts_with" } }]);

  await speak(page, "List Lambda functions containing etl in Mumbai.");
  await expect(page.locator(".voice-call.ok")).toContainText("search_resources");
  expect(calls.at(-1)).toEqual({ name: "search_resources", args: { text: "etl", match: "contains", services: ["lambda"], region: "ap-south-1" } });
  expect(await lastSpoken(page)).toContain("It is: nightly-etl.");

  const before = calls.length;
  for (const q of ["Add Environment as Production and Owner as Backend to all three.", "Yes, apply these tags."]) {
    msg = await type(page, q);
    await expect(msg).toContainText("I can't change tags from here");
    await speak(page, q);
    await expect(page.locator(".voice-reply")).toContainText("I can't change tags from here");
  }
  expect(calls.length).toBe(before); // no tool call at all, so certainly no write
});
