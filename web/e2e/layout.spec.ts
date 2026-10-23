// The redesigned shell against the real fixture-mode server: every page renders from the sidebar, the sidebar
// collapses with labelled icons, the connect form validates, and nothing scrolls sideways on desktop, tablet or phone.
import { expect, test, type Page } from "@playwright/test";

const ALEXA = process.env.CG_E2E_ALEXA_TOKEN!;
const PAGES: [string, string][] = [["Overview", "Overview"], ["Cost Intelligence", "Cost Intelligence"],
  ["Resource Governance", "Resource Governance"], ["Voice Assistant", "Voice Assistant"], ["Tag Management", "Tag Management"],
  ["Activity & Audit", "Activity & Audit"], ["Settings", "Settings"], ["MCP Explorer", "MCP Explorer"]];

async function connect(page: Page) {
  await page.goto("/");
  await page.getByLabel("Alexa bearer token").fill(ALEXA);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.locator(".conn-pill")).toHaveText("● Connected");
}

const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);

test("connect screen: validation messages, then the connected-state confirmation", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Connect your AWS governance assistant" })).toBeVisible();
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByText("Paste the Alexa bearer token (alexa:read).")).toBeVisible(); // nothing was sent
  await page.getByLabel("MCP server URL").fill("ftp://x");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByText("Use an http:// or https:// URL, or /mcp.")).toBeVisible();
  await page.getByLabel("MCP server URL").fill("/mcp");
  await page.getByLabel("Alexa bearer token").fill(ALEXA);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.locator(".connected-banner")).toContainText("tools discovered with tools/list");
  expect(await page.content()).not.toContain(ALEXA);
});

test("sidebar: every page renders, with a breadcrumb and the kill-switch status", async ({ page }) => {
  await connect(page);
  await expect(page.getByLabel("Kill switch status")).toContainText("Kill switch not engaged");
  const nav = page.getByRole("navigation", { name: "Main" });
  for (const [label, heading] of PAGES) {
    await nav.getByRole("button", { name: label }).click();
    await expect(nav.getByRole("button", { name: label })).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toContainText(label);
    expect(await overflow(page)).toBeLessThanOrEqual(0);
  }
});

test("sidebar: collapses to icons that keep their accessible names", async ({ page }) => {
  await connect(page);
  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  await expect(page.locator(".sidebar.collapsed")).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Main" });
  await nav.getByRole("button", { name: "Tag Management" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Tag Management" })).toBeVisible();
  await expect(nav.getByRole("button", { name: "Tag Management" })).toHaveAttribute("data-tip", "Tag Management");
  await page.getByRole("button", { name: "Expand sidebar" }).click();
  await expect(page.locator(".sidebar.collapsed")).toHaveCount(0);
});

for (const [name, viewport] of [["tablet", { width: 820, height: 1100 }], ["mobile", { width: 375, height: 812 }]] as const) {
  test(`responsive (${name}): menu navigation and no horizontal overflow on any page`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto("/");
    expect(await overflow(page)).toBeLessThanOrEqual(0);
    await connect(page);
    for (const [label, heading] of PAGES) {
      await page.getByRole("button", { name: "Menu" }).click();
      await page.getByRole("navigation", { name: "Main" }).getByRole("button", { name: label }).click();
      await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
      await page.waitForTimeout(300); // panels load their (fixture) data
      expect(await overflow(page), `${label} overflows at ${viewport.width}px`).toBeLessThanOrEqual(0);
    }
  });
}
