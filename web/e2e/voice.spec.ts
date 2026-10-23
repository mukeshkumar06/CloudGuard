// Voice flow against the real fixture-mode server through the real Vite proxy. Only the browser's speech APIs are
// replaced (Playwright's Chromium has no speech service); the MCP transport is not mocked.
import { expect, test, type Page } from "@playwright/test";

const ALEXA = process.env.CG_E2E_ALEXA_TOKEN!;

// Installed before the app loads. Recognition sessions and utterances are recorded on window.__voice so the test can
// "speak" and observe what would be said aloud.
function installFakeSpeech(opts: { recognition: boolean }) {
  type Rec = { onresult: ((e: unknown) => void) | null; onerror: ((e: unknown) => void) | null; onend: (() => void) | null };
  const v = { recs: [] as Rec[], spoken: [] as string[], active: null as null | { onend: (() => void) | null; onerror: (() => void) | null }, overlaps: 0 };
  (window as unknown as { __voice: typeof v }).__voice = v;
  class FakeRecognition {
    lang = ""; interimResults = false; continuous = false; maxAlternatives = 1;
    onresult = null; onerror = null; onend = null;
    onstart: (() => void) | null = null;
    start() { v.recs.push(this as unknown as Rec); this.onstart?.(); } // the microphone opens at once
    stop() { (this as unknown as Rec).onend?.(); }
    abort() { (this as unknown as Rec).onerror?.({ error: "aborted" }); (this as unknown as Rec).onend?.(); }
  }
  class FakeUtterance { lang = ""; onend = null; onerror = null; constructor(public text: string) {} }
  const synth = {
    speak(u: FakeUtterance) { if (v.active) v.overlaps++; v.active = u; v.spoken.push(u.text); },
    cancel() { const u = v.active; v.active = null; u?.onerror?.(); },
  };
  const w = window as unknown as Record<string, unknown>;
  delete w.SpeechRecognition;
  delete w.webkitSpeechRecognition;
  if (opts.recognition) w.webkitSpeechRecognition = FakeRecognition;
  Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });
  w.SpeechSynthesisUtterance = FakeUtterance;
}

async function say(page: Page, text: string) {
  await page.getByRole("button", { name: "Start voice input" }).click();
  await expect(page.locator(".voice")).toHaveAttribute("data-status", "listening");
  await page.evaluate((t) => {
    const v = (window as unknown as { __voice: { recs: { onresult: (e: unknown) => void; onend: () => void }[] } }).__voice;
    const r = v.recs[v.recs.length - 1];
    r.onresult({ resultIndex: 0, results: [Object.assign([{ transcript: t.slice(0, 6) }], { isFinal: false })] });
    r.onresult({ resultIndex: 0, results: [Object.assign([{ transcript: t }], { isFinal: true })] });
    r.onend();
  }, text);
}

const spoken = (page: Page) => page.evaluate(() => (window as unknown as { __voice: { spoken: string[] } }).__voice.spoken);
const finishSpeaking = (page: Page) => page.evaluate(() => {
  const v = (window as unknown as { __voice: { active: { onend: (() => void) | null } | null } }).__voice;
  const u = v.active;
  v.active = null;
  u?.onend?.();
});

// CloudGuard's MCP surface has no state-changing tool, so the confirmation walkthroughs make the browser see the named
// tools as NOT read-only: readOnlyHint is removed from the real server's tools/list answer on its way to the page.
// The server and its tools are unchanged; the calls that do run are real.
async function treatAsStateChanging(page: Page, tools: string[]) {
  const strip = (msg: { result?: { tools?: { name: string; annotations?: Record<string, unknown> }[] } }) => {
    for (const t of msg.result?.tools ?? []) if (tools.includes(t.name) && t.annotations) delete t.annotations.readOnlyHint;
    return msg;
  };
  await page.route("**/mcp", async (route) => {
    const req = route.request();
    if (req.method() !== "POST" || JSON.parse(req.postData() ?? "{}").method !== "tools/list") return route.continue();
    const res = await route.fetch();
    const body = await res.text();
    const out = (res.headers()["content-type"] ?? "").includes("text/event-stream")
      ? body.replace(/^data: (.*)$/gm, (_, json: string) => `data: ${JSON.stringify(strip(JSON.parse(json)))}`)
      : JSON.stringify(strip(JSON.parse(body)));
    await route.fulfill({ response: res, body: out });
  });
}

// These walkthroughs drive one exchange at a time, so they switch hands-free Auto-listen off (see the last test).
async function open(page: Page, recognition = true, autoListen = false, stateChanging: string[] = []): Promise<string[]> {
  if (stateChanging.length) await treatAsStateChanging(page, stateChanging);
  const toolCalls: string[] = [];
  page.on("request", (r) => {
    if (r.method() !== "POST" || new URL(r.url()).pathname !== "/mcp") return;
    const body = JSON.parse(r.postData() ?? "{}");
    if (body.method === "tools/call") toolCalls.push(body.params.name);
  });
  await page.addInitScript(installFakeSpeech, { recognition });
  await page.goto("/");
  await page.getByLabel("Alexa bearer token").fill(ALEXA);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.locator(".conn-pill")).toHaveText("● Connected");
  if (recognition && !autoListen) await page.getByLabel("Auto-listen").uncheck();
  return toolCalls;
}

test("voice: a read-only question runs exactly one MCP call at once, with no confirmation, and speaks the server speech", async ({ page }) => {
  const calls = await open(page);
  const card = page.getByRole("group", { name: "Pending tool call" });
  await say(page, "How much did AWS cost last month?");
  await expect(page.locator(".voice-transcript")).toContainText("How much did AWS cost last month?");
  await expect(page.locator(".voice-call.ok")).toContainText("get_monthly_aws_bill");
  expect(calls).toEqual(["get_monthly_aws_bill"]);
  for (const s of await spoken(page)) expect(s).not.toContain("Shall I continue?");
  // The spoken text is exactly the speech field the server returned, as shown in the Results panel.
  const serverSpeech = (await page.locator(".tool-pane .result .speech-text").first().textContent())!.replace(/^“|”$/g, "");
  const said = await spoken(page);
  expect(said.at(-1)).toBe(serverSpeech);
  expect(said.at(-1)).toContain("billing month");
  await expect(page.locator(".voice-reply")).toContainText(serverSpeech);
  await expect(card).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __voice: { overlaps: number } }).__voice.overlaps)).toBe(0);
  // Nothing spoken contains the token or structured JSON.
  for (const s of said) {
    expect(s).not.toContain(ALEXA);
    expect(s).not.toMatch(/[{}]/);
  }

  // Replay, stop and mute.
  await finishSpeaking(page);
  await expect(page.locator(".voice")).toHaveAttribute("data-status", "idle");
  await page.getByRole("button", { name: "Replay" }).click();
  expect((await spoken(page)).at(-1)).toBe(serverSpeech);
  await expect(page.locator(".voice")).toHaveAttribute("data-status", "speaking");
  await page.getByRole("button", { name: "Stop speaking" }).click();
  await expect(page.locator(".voice")).toHaveAttribute("data-status", "idle");
  // Voice picker: switching and previewing runs no tool and leaves the reply and replay text alone.
  await page.getByRole("button", { name: "Male", exact: true }).click();
  await expect(page.getByRole("button", { name: "Male", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Preview voice" }).click();
  expect((await spoken(page)).at(-1)).toContain("This is how I will sound");
  await expect(page.locator(".voice-reply")).toContainText(serverSpeech);
  await finishSpeaking(page);
  await page.getByRole("button", { name: "Mute" }).click();
  await expect(page.getByRole("button", { name: "Replay" })).toBeDisabled();
  expect(calls).toEqual(["get_monthly_aws_bill"]);
});

test("voice: a state-changing call waits; verbal cancel runs zero MCP calls; the button confirms once", async ({ page }) => {
  const calls = await open(page, true, false, ["scan_tag_compliance"]);
  await say(page, "Check my tagging compliance.");
  await expect(page.getByRole("group", { name: "Pending tool call" })).toContainText("scan_tag_compliance");
  await say(page, "cancel");
  await expect(page.locator(".voice-reply")).toContainText("Okay, cancelled. Nothing was run.");
  await expect(page.getByRole("group", { name: "Pending tool call" })).toHaveCount(0);
  expect(calls).toEqual([]);

  await say(page, "Check my tagging compliance.");
  await page.getByRole("button", { name: "Yes, run it" }).click();
  await expect(page.locator(".voice-call.ok")).toContainText("scan_tag_compliance");
  expect(calls).toEqual(["scan_tag_compliance"]);
  expect((await spoken(page)).at(-1)).toContain("missing mandatory tags");
});

test("voice: permission denied and empty transcript show errors and run nothing", async ({ page }) => {
  const calls = await open(page);
  await page.getByRole("button", { name: "Start voice input" }).click();
  await page.evaluate(() => {
    const v = (window as unknown as { __voice: { recs: { onerror: (e: unknown) => void; onend: () => void }[] } }).__voice;
    const r = v.recs[v.recs.length - 1];
    r.onerror({ error: "not-allowed" });
    r.onend();
  });
  await expect(page.locator(".voice-error-text")).toContainText("Microphone access was denied");
  await say(page, "   ");
  // An empty transcript is not an answer: the browser's end restarts listening (within the minute window)...
  await expect(page.locator(".voice")).toHaveAttribute("data-status", "listening");
  await expect(page.locator(".voice-status")).toContainText("Listening");
  // ...until the user stops, which with nothing heard is "didn't hear", never a question or an answer.
  await page.getByRole("button", { name: "Done — stop listening" }).click();
  await page.evaluate(() => {
    const v = (window as unknown as { __voice: { recs: { onend: () => void }[] } }).__voice;
    v.recs[v.recs.length - 1].onend();
  });
  await expect(page.locator(".voice-error-text")).toContainText("Didn't hear a question");
  await expect(page.locator(".voice-status")).toContainText("Stopped");
  expect(calls).toEqual([]);
});

test("voice: unsupported browser disables the microphone but typing still works", async ({ page }) => {
  await open(page, false);
  await expect(page.getByRole("button", { name: "Start voice input" })).toBeDisabled();
  await expect(page.locator(".voice")).toContainText("Voice input needs a browser with the Web Speech API");
  await expect(page.getByPlaceholder("Ask about your AWS bill or resource tagging…")).toBeEnabled();
});

/** Speak into the session that is already open (hands-free mode starts it without a click). */
async function answer(page: Page, text: string) {
  await expect(page.locator(".voice")).toHaveAttribute("data-status", "listening");
  await page.evaluate((t) => {
    const v = (window as unknown as { __voice: { recs: { onresult: (e: unknown) => void; onend: () => void }[] } }).__voice;
    const r = v.recs[v.recs.length - 1];
    r.onresult({ resultIndex: 0, results: [Object.assign([{ transcript: t }], { isFinal: true })] });
    r.onend();
  }, text);
}

const sessions = (page: Page) => page.evaluate(() => (window as unknown as { __voice: { recs: unknown[] } }).__voice.recs.length);

test("voice: hands-free conversation listens after each prompt and answer, and every state-changing call needs its own yes", async ({ page }) => {
  const calls = await open(page, true, true, ["get_monthly_aws_bill", "scan_tag_compliance"]);
  await expect(page.getByLabel("Auto-listen")).toBeChecked();
  await say(page, "How much did AWS cost last month?");
  await finishSpeaking(page); // the confirmation prompt ends
  await expect(page.locator(".voice-status")).toContainText("Listening for confirmation");
  await answer(page, "maybe"); // ambiguous: asks again, runs nothing
  await expect(page.locator(".voice-reply")).toContainText("didn't catch a yes or no");
  expect(calls).toEqual([]);
  await finishSpeaking(page);
  await answer(page, "yes");
  await expect(page.locator(".voice-call.ok")).toContainText("get_monthly_aws_bill");
  expect(calls).toEqual(["get_monthly_aws_bill"]);
  const serverSpeech = (await page.locator(".tool-pane .result .speech-text").first().textContent())!.replace(/^“|”$/g, "");
  expect((await spoken(page)).at(-1)).toBe(serverSpeech);

  await finishSpeaking(page); // the answer ends; the follow-up question is asked
  expect((await spoken(page)).at(-1)).toBe("Would you like to know anything else?");
  await finishSpeaking(page);
  await expect(page.locator(".voice-status")).toContainText("Listening…");
  await answer(page, "Check my tagging compliance.");
  await expect(page.getByRole("group", { name: "Pending tool call" })).toContainText("scan_tag_compliance");
  expect(calls).toEqual(["get_monthly_aws_bill"]); // the new question waits for its own confirmation
  await finishSpeaking(page);
  await answer(page, "no");
  await expect(page.locator(".voice-reply")).toContainText("Nothing was run");
  expect(calls).toEqual(["get_monthly_aws_bill"]);

  // Disconnect while listening: the session is stopped and nothing else starts.
  await finishSpeaking(page);
  await expect(page.locator(".voice")).toHaveAttribute("data-status", "listening");
  const before = await sessions(page);
  await page.getByRole("button", { name: "Disconnect" }).click();
  await expect(page.locator(".conn-pill")).toHaveText("○ Not connected");
  await page.waitForTimeout(1000);
  expect(await sessions(page)).toBe(before);
  expect(calls).toEqual(["get_monthly_aws_bill"]);
});
