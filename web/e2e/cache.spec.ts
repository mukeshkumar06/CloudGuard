// Conversation answer cache against the real fixture-mode server through the real Vite proxy. Only the browser's
// speech APIs are replaced (Playwright's Chromium has no speech service); MCP calls are real and counted.
import { expect, test, type Page } from "@playwright/test";

const ALEXA = process.env.CG_E2E_ALEXA_TOKEN!;

function installFakeSpeech() {
  type Rec = { onresult: ((e: unknown) => void) | null; onend: (() => void) | null; onstart: (() => void) | null };
  const v = { recs: [] as Rec[], spoken: [] as string[], active: null as null | { onend: (() => void) | null; onerror: (() => void) | null } };
  (window as unknown as { __voice: typeof v }).__voice = v;
  class FakeRecognition {
    lang = ""; interimResults = false; continuous = false; maxAlternatives = 1;
    onresult = null; onerror = null; onend: (() => void) | null = null; onstart: (() => void) | null = null;
    start() { v.recs.push(this as unknown as Rec); this.onstart?.(); }
    stop() { this.onend?.(); }
    abort() { this.onend?.(); }
  }
  class FakeUtterance { lang = ""; onend = null; onerror = null; constructor(public text: string) {} }
  const synth = {
    speak(u: FakeUtterance) { v.active = u; v.spoken.push(u.text); },
    cancel() { const u = v.active; v.active = null; u?.onerror?.(); },
  };
  const w = window as unknown as Record<string, unknown>;
  delete w.SpeechRecognition;
  w.webkitSpeechRecognition = FakeRecognition;
  Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });
  w.SpeechSynthesisUtterance = FakeUtterance;
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

const spoken = (page: Page) => page.evaluate(() => (window as unknown as { __voice: { spoken: string[] } }).__voice.spoken);

async function connect(page: Page) {
  await page.getByLabel("Alexa bearer token").fill(ALEXA);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.locator(".conn-pill")).toHaveText("● Connected");
  await page.getByLabel("Auto-listen").uncheck(); // one exchange at a time
}

async function type(page: Page, q: string) {
  await page.getByPlaceholder("Ask about your AWS bill or resource tagging…").fill(q);
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  return page.locator(".msg.assistant").last();
}

test("cache: typed and spoken equivalent questions reuse one answer; Refresh and reconnect fetch fresh", async ({ page }) => {
  const calls: string[] = [];
  page.on("request", (r) => {
    if (r.method() !== "POST" || new URL(r.url()).pathname !== "/mcp") return;
    const body = JSON.parse(r.postData() ?? "{}");
    if (body.method === "tools/call") calls.push(body.params.name);
  });
  await page.addInitScript(installFakeSpeech);
  await page.goto("/");
  await connect(page);

  // First ask: a real call, at once (read-only, no confirmation).
  let msg = await type(page, "Which AWS account am I using?");
  await expect(msg.locator(".result-head .pill").first()).toContainText("Succeeded", { timeout: 30_000 });
  expect(calls).toEqual(["get_aws_account_identity"]);
  const speech = (await msg.locator(".speech-text").textContent())!.replace(/^“|”$/g, "");

  // An equivalent typed question: reused at once, no Confirm needed, no server call.
  msg = await type(page, "Show me my AWS account ID.");
  await expect(msg.locator(".cache-note")).toContainText("Reused answer");
  await expect(msg.locator(".cache-note")).toContainText("no server or AWS call was made");
  await expect(msg.getByRole("button", { name: "Confirm and call" })).toHaveCount(0);
  await expect(msg.locator(".speech-text")).toHaveText(`“${speech}”`);
  await expect(page.locator(".tool-pane .result .pill", { hasText: "Cached · reused" })).toHaveCount(1);
  expect(calls).toEqual(["get_aws_account_identity"]);

  // Voice shares the same cache, speaks the cached speech, and shows the indicator.
  await speak(page, "Who am I logged in as?");
  await expect(page.locator(".voice .cache-note")).toContainText("Reused answer");
  await expect(page.locator(".voice-call.cached")).toContainText("get_aws_account_identity");
  expect((await spoken(page)).at(-1)).toContain(speech);
  expect((await spoken(page)).at(-1)).toContain("I already have this answer");
  await expect(page.getByRole("group", { name: "Pending tool call" })).toHaveCount(0);
  expect(calls).toEqual(["get_aws_account_identity"]);

  // Voice Refresh: read-only, so one fresh call at once with no confirmation.
  await page.locator(".voice").getByRole("button", { name: "Refresh data" }).click();
  await expect(page.locator(".voice-call.ok")).toContainText("get_aws_account_identity");
  await expect(page.getByRole("group", { name: "Pending tool call" })).toHaveCount(0);
  expect(calls).toEqual(["get_aws_account_identity", "get_aws_account_identity"]);

  // Typed Refresh data on the reused message: one fresh call.
  await msg.getByRole("button", { name: "Refresh data" }).click();
  await expect(msg.locator(".cache-note")).toHaveCount(0);
  await expect(msg.locator(".result-head .pill").first()).toContainText("Succeeded", { timeout: 30_000 });
  expect(calls).toEqual(["get_aws_account_identity", "get_aws_account_identity", "get_aws_account_identity"]);

  // A different question is not answered from the cache: it makes its own call.
  msg = await type(page, "What was my AWS bill last month?");
  await expect(msg.locator(".result-head .pill").first()).toContainText("Succeeded", { timeout: 30_000 });
  await expect(msg.locator(".cache-note")).toHaveCount(0);
  expect(calls.at(-1)).toBe("get_monthly_aws_bill");

  // Disconnect clears the cache: after reconnecting, the same question needs a real call again.
  await page.getByRole("button", { name: "Disconnect" }).click();
  await connect(page);
  msg = await type(page, "Which AWS account am I using?");
  await expect(msg.locator(".result-head .pill").first()).toContainText("Succeeded", { timeout: 30_000 });
  await expect(msg.locator(".cache-note")).toHaveCount(0);
  expect(calls.length).toBe(5);

  // Nothing about the cache reaches browser storage.
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length + document.cookie.length)).toBe(0);
});
