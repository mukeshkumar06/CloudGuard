// "Which AWS account am I using?" against the real fixture-mode server through the real Vite proxy. Nothing is mocked
// except the browser speech APIs in the voice test (Playwright's Chromium has no speech service).
import { readFileSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

const ALEXA = process.env.CG_E2E_ALEXA_TOKEN!;
const QUESTION = "Which AWS account am I using?";

async function open(page: Page): Promise<string[]> {
  const toolCalls: string[] = [];
  page.on("request", (r) => {
    if (r.method() !== "POST" || new URL(r.url()).pathname !== "/mcp") return;
    const body = JSON.parse(r.postData() ?? "{}");
    if (body.method === "tools/call") toolCalls.push(body.params.name);
  });
  await page.goto("/");
  await page.getByLabel("Alexa bearer token").fill(ALEXA);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.locator(".conn-pill")).toHaveText("● Connected");
  return toolCalls;
}

test("identity: the suggested question routes to get_aws_account_identity and runs without confirmation", async ({ page }) => {
  const calls = await open(page);
  await page.getByRole("button", { name: QUESTION }).click();
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  const plan = page.locator(".msg.assistant .plan").last();
  await expect(plan).toContainText("get_aws_account_identity");
  await expect(plan.getByRole("button", { name: "Confirm and call" })).toHaveCount(0); // read-only: no confirmation
  const result = plan.locator(".result");
  await expect(result.locator(".result-head .pill").first()).toContainText("Succeeded", { timeout: 30_000 });
  expect(calls).toEqual(["get_aws_account_identity"]);

  const raw = JSON.parse((await result.locator(".raw pre").textContent()) ?? "{}");
  const out = raw.structuredContent;
  // Fixture mode: clearly synthetic, account and type only.
  expect(out).toMatchObject({ schema_version: "alexa-1", tool: "get_aws_account_identity", mode: "fixture",
                              synthetic: true, outcome: "answered", account_id_last4: "9012",
                              identity_type: "assumed_role", identity_label: "IAM role" });
  expect(out.account_id).toMatch(/^\d{12}$/);
  // The spoken and structured answers agree.
  await expect(result.locator(".speech-text")).toContainText(`ends in ${out.account_id_last4}`);
  await expect(result.locator(".speech-text")).toContainText("an IAM role");
  await expect(result.locator(".speech-text")).toContainText("synthetic");
  // Never the principal ARN, role or session name, or UserId.
  const text = JSON.stringify(raw);
  for (const leak of ["arn:", "CloudGuardDemoReadOnly", "cloudguard-demo-session", "principal_arn", "display_label",
                      "UserId", ALEXA]) {
    expect(text).not.toContain(leak);
  }
});

test("identity: fixture mode made zero AWS API calls", async () => {
  expect(readFileSync(process.env.CG_E2E_AWS_LOG!, "utf8")).toBe("");
});
