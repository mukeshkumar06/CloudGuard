// Region-aware inventory against the real fixture-mode server through the real Vite proxy (fixture region:
// ap-south-1). Nothing is mocked; tool calls and their arguments are read from the real requests.
import { expect, test, type Page } from "@playwright/test";

const ALEXA = process.env.CG_E2E_ALEXA_TOKEN!;

async function open(page: Page) {
  const calls: { name: string; args: Record<string, unknown> }[] = [];
  page.on("request", (r) => {
    if (r.method() !== "POST" || new URL(r.url()).pathname !== "/mcp") return;
    const body = JSON.parse(r.postData() ?? "{}");
    if (body.method === "tools/call") calls.push({ name: body.params.name, args: body.params.arguments ?? {} });
  });
  await page.goto("/");
  await page.getByLabel("Alexa bearer token").fill(ALEXA);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.locator(".conn-pill")).toHaveText("● Connected");
  return calls;
}

async function type(page: Page, q: string) {
  await page.getByPlaceholder("Ask about your AWS bill or resource tagging…").fill(q);
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  return page.locator(".msg.assistant").last();
}

test("region: a broad question asks which region first; an unknown region asks, then 'Mumbai' scans ap-south-1", async ({ page }) => {
  const calls = await open(page);
  let msg = await type(page, "Show me my AWS resources.");
  await expect(msg).toContainText("Which AWS region should I search?");
  expect(calls).toEqual([]); // no discovery before the region is known
  msg = await type(page, "All regions");
  await expect(msg).toContainText("Searching your AWS resources in all supported regions.");
  await expect(msg.locator(".result-head .pill").first()).toContainText("Succeeded", { timeout: 30_000 });
  expect(calls).toEqual([{ name: "get_resource_inventory", args: { region: "all" } }]);
  calls.length = 0;

  msg = await type(page, "Show me my AWS resources in Atlantis.");
  await expect(msg).toContainText("Which AWS region should I search?");
  expect(calls).toEqual([]); // an unknown region is never scanned as all regions

  msg = await type(page, "Mumbai");
  await expect(msg.locator(".result-head .pill").first()).toContainText("Succeeded", { timeout: 30_000 });
  expect(calls).toEqual([{ name: "get_resource_inventory", args: { region: "ap-south-1" } }]);
  const raw = JSON.parse((await msg.locator(".raw pre").textContent()) ?? "{}");
  expect(raw.structuredContent.regions_scanned).toEqual(["ap-south-1"]);
});

test("region: an unknown region asks again; a service-specific question scans only that service", async ({ page }) => {
  const calls = await open(page);
  await type(page, "What resources do I have in Atlantis?");
  let msg = await type(page, "Atlantis");
  await expect(msg).toContainText("didn't recognise that region");
  expect(calls).toEqual([]);

  msg = await type(page, "Show my Lambda functions in ap-south-1");
  await expect(msg.locator(".result-head .pill").first()).toContainText("Succeeded", { timeout: 30_000 });
  expect(calls).toEqual([{ name: "get_resource_inventory", args: { region: "ap-south-1", services: ["lambda"] } }]);
});

test("region: a region chip answers the question; a region the account doesn't use is a clear error", async ({ page }) => {
  const calls = await open(page);
  let msg = await type(page, "Give me my infrastructure overview in Narnia.");
  await msg.getByRole("button", { name: "All supported regions" }).click();
  msg = page.locator(".msg.assistant").last();
  await expect(msg.locator(".result-head .pill").first()).toContainText("Succeeded", { timeout: 30_000 });
  expect(calls.at(-1)).toEqual({ name: "get_resource_inventory", args: { region: "all" } });

  msg = await type(page, "Show my resources in Ireland");
  await expect(msg.locator(".result")).toContainText("region not enabled for this account: eu-west-1", { timeout: 30_000 });
});
