// One serial walkthrough of the simulator against the real fixture-mode server. No route is mocked: every result
// comes from the CloudGuard backend through the Vite proxy.
import { readFileSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

const ALEXA = process.env.CG_E2E_ALEXA_TOKEN!;
const OPERATOR = process.env.CG_E2E_OPERATOR_TOKEN!;
const COST_TOOLS = ["get_monthly_aws_bill", "get_current_month_spending", "get_cost_breakdown", "compare_monthly_costs",
  "investigate_cost_increase", "get_cost_anomalies"];
const GOV_TOOLS = ["get_resource_inventory", "search_resources", "scan_tag_compliance", "list_untagged_resources", "explain_finding",
  "recommend_tags", "preview_tag_changes"];
const ACCOUNT_TOOLS = ["get_aws_account_identity"];
const FIXTURE_ACCOUNT = "123456789012";

test.describe.configure({ mode: "serial" });

let page: Page;
const consoleText: string[] = [];
const mcpPosts: string[] = [];

test.beforeAll(async ({ browser }) => {
  page = await browser.newPage();
  page.on("console", (m) => consoleText.push(m.text()));
  page.on("pageerror", (e) => consoleText.push(String(e)));
  page.on("request", (r) => {
    if (r.method() === "POST" && new URL(r.url()).pathname === "/mcp") mcpPosts.push(r.postData() ?? "");
  });
});

test.afterAll(async () => {
  await page.close();
});

async function connectWith(token: string) {
  await page.getByLabel("Alexa bearer token").fill(token);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
}

// Tool discovery and direct calls live on the Developer tools page (the redesign's main page is the assistant).
async function openDeveloper() {
  await page.getByRole("navigation", { name: "Main" }).getByRole("button", { name: "MCP Explorer" }).click();
}

async function callTool(name: string): Promise<ReturnType<Page["locator"]>> {
  await openDeveloper();
  await page.locator(`nav button[title="${name}"]`).click();
  await page.getByRole("button", { name: `Call ${name}` }).click();
  const result = page.locator(".tool-pane .result").first();
  await expect(result.locator(".tool-name")).toHaveText(name);
  await expect(result.locator(".result-head .pill").first()).toContainText("Succeeded", { timeout: 30_000 });
  return result;
}

test("1-2. opens disconnected", async () => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Connect your AWS governance assistant" })).toBeVisible();
  await expect(page.locator(".conn-pill")).toHaveText("○ Not connected");
  await expect(page.locator("nav[aria-label='Discovered tools']")).toHaveCount(0);
});

test("3. rejects an incorrect Alexa token", async () => {
  await connectWith("not-the-right-token");
  await expect(page.getByRole("alert")).toContainText("401 Unauthorized");
  await expect(page.getByLabel("Alexa bearer token")).toHaveValue("");
});

test("security: rejects the operator token", async () => {
  await connectWith(OPERATOR);
  await expect(page.getByRole("alert")).toContainText("does not receive CloudGuard's Alexa response projection");
  await expect(page.locator(".conn-pill")).toHaveText("○ Not connected");
});

test("4-7. connects with the Alexa token and discovers exactly fourteen tools", async () => {
  await connectWith(ALEXA);
  await expect(page.locator(".conn-pill")).toHaveText("● Connected");
  await expect(page.locator(".mode-banner.fixture")).toContainText("FIXTURE MODE");
  await expect(page.getByText("protocol 2025-11-25").first()).toBeVisible();
  await openDeveloper();
  const tools = page.locator("nav[aria-label='Discovered tools'] button");
  await expect(tools).toHaveCount(14);
  expect((await tools.evaluateAll((els) => els.map((e) => e.getAttribute("title")))).sort())
    .toEqual([...COST_TOOLS, ...GOV_TOOLS, ...ACCOUNT_TOOLS].sort());
  const groups = page.locator(".tool-group");
  await expect(groups.locator("h3")).toHaveText(["Cost Intelligence", "Resource Governance", "Account"]);
  for (const t of COST_TOOLS) await expect(groups.nth(0).locator(`button[title="${t}"]`)).toBeVisible();
  for (const t of GOV_TOOLS) await expect(groups.nth(1).locator(`button[title="${t}"]`)).toBeVisible();
  for (const t of ACCOUNT_TOOLS) await expect(groups.nth(2).locator(`button[title="${t}"]`)).toBeVisible();
  await expect(page.getByLabel("Alexa bearer token")).toHaveCount(0);
});

test("8-9. get_monthly_aws_bill renders speech and structured evidence", async () => {
  const r = await callTool("get_monthly_aws_bill");
  await expect(r.locator(".speech-text")).toContainText("billing month");
  await expect(r.locator(".evidence")).toContainText("calculated net");
  await expect(r.locator(".evidence")).toContainText("top services");
  await expect(r.locator(".pill.fixture")).toHaveText("FIXTURE DATA");
});

test("10. get_resource_inventory", async () => {
  const r = await callTool("get_resource_inventory");
  await expect(r.locator(".speech-text")).toContainText("CloudGuard found 8 resources");
  await expect(r.locator(".evidence")).toContainText("by service");
});

test("11. scan_tag_compliance", async () => {
  const r = await callTool("scan_tag_compliance");
  await expect(r.locator(".speech-text")).toContainText("missing mandatory tags");
  await expect(r.locator(".evidence")).toContainText("missing mandatory tags");
});

let findingId = "";

test("12. list_untagged_resources", async () => {
  const r = await callTool("list_untagged_resources");
  await expect(r.locator(".speech-text")).toContainText("missing mandatory tags");
  const raw = JSON.parse(await r.locator(".raw pre").textContent() ?? "{}");
  findingId = raw.structuredContent.resources[0].finding_id;
  expect(findingId).toMatch(/^tagcomp-[0-9a-f]{16}$/);
});

test("13. recommend_tags with a finding ID returned by the server", async () => {
  await page.locator("nav button[title='recommend_tags']").click();
  await page.locator(".picker .chip").first().click();
  await expect(page.locator(".explorer input").first()).toHaveValue(findingId);
  await page.getByRole("button", { name: "Call recommend_tags" }).click();
  const r = page.locator(".tool-pane .result").first();
  await expect(r.locator(".result-head .pill").first()).toContainText("Succeeded", { timeout: 30_000 });
  await expect(r.locator(".speech-text")).toContainText("Nothing was changed");
  const raw = JSON.parse(await r.locator(".raw pre").textContent() ?? "{}");
  expect(raw.structuredContent.finding_id).toBe(findingId);
  expect(raw.structuredContent.authorizes_change).toBe(false);
});

test("14. malformed JSON is rejected before any network call", async () => {
  const before = mcpPosts.length;
  await page.getByRole("button", { name: "Edit raw JSON" }).click();
  await page.locator(".explorer textarea").fill('{"resource": ');
  await page.getByRole("button", { name: "Call recommend_tags" }).click();
  await expect(page.locator(".errors")).toContainText("invalid JSON");
  expect(mcpPosts.length).toBe(before);
});

test("15. an unauthorized argument is rejected before any network call", async () => {
  const before = mcpPosts.length;
  await page.locator(".explorer textarea").fill(JSON.stringify({ resource: findingId, account_id: "999988887777" }));
  await page.getByRole("button", { name: "Call recommend_tags" }).click();
  await expect(page.locator(".errors")).toContainText("account_id: not accepted by this tool");
  expect(mcpPosts.length).toBe(before);
});

test("security: Alexa cannot invoke an unapproved tool, even bypassing the UI", async () => {
  const res = await page.request.post("/mcp", {
    headers: { Authorization: `Bearer ${ALEXA}`, Accept: "application/json, text/event-stream",
               "Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25" },
    data: { jsonrpc: "2.0", id: 9, method: "tools/call", params: { name: "approve_fix", arguments: {} } },
  });
  const body = await res.json();
  expect(body.result.isError).toBe(true);
  expect(body.result.content[0].text).toBe("not authorized to call approve_fix");
});

test("security: projected responses carry no excluded sensitive fields", async () => {
  const raws = await page.locator(".tool-pane .result .raw pre").allTextContents();
  expect(raws.length).toBeGreaterThanOrEqual(5);
  for (const raw of raws) {
    expect(raw).not.toContain(FIXTURE_ACCOUNT);
    expect(raw).not.toContain("arn:aws");
    for (const key of ["account_id", "resource_arn", "resource_name", "data_source", "observed_tags", "policy_source"]) {
      expect(raw).not.toContain(`"${key}"`);
    }
  }
});

test("16. disconnect clears the token and all results", async () => {
  await page.getByRole("button", { name: "Disconnect" }).click();
  await expect(page.locator(".conn-pill")).toHaveText("○ Not connected");
  await expect(page.getByLabel("Alexa bearer token")).toHaveValue("");
  await expect(page.locator(".result")).toHaveCount(0);
  const leaks = await page.evaluate((t) => ({
    html: document.documentElement.outerHTML.includes(t),
    inputs: [...document.querySelectorAll("input,textarea")].some((i) => (i as HTMLInputElement).value.includes(t)),
    url: location.href.includes(t),
    storage: localStorage.length + sessionStorage.length,
    cookie: document.cookie,
  }), ALEXA);
  expect(leaks).toEqual({ html: false, inputs: false, url: false, storage: 0, cookie: "" });
});

test("security: the token never appears in console output or a request body", async () => {
  expect(consoleText.some((t) => t.includes(ALEXA) || t.includes(OPERATOR))).toBe(false);
  expect(mcpPosts.length).toBeGreaterThan(0);
  expect(mcpPosts.some((b) => b.includes(ALEXA) || b.includes(OPERATOR))).toBe(false);
});

test("layout: fits a 1366x768 desktop without horizontal scrolling", async () => {
  await connectWith(ALEXA);
  await callTool("get_monthly_aws_bill");
  const widths = await page.evaluate(() => ({ doc: document.documentElement.scrollWidth, win: window.innerWidth }));
  expect(widths.doc).toBeLessThanOrEqual(widths.win);
  await page.locator(".speech-text").first().scrollIntoViewIfNeeded();
  await expect(page.locator(".speech-text").first()).toBeInViewport();
});

test("security: fixture mode made zero AWS API calls", async () => {
  expect(readFileSync(process.env.CG_E2E_AWS_LOG!, "utf8")).toBe("");
});
