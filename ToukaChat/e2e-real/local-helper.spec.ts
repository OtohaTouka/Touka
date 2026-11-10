import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { API_ORIGIN } from "./lib/constants";
import { loginInBrowser, openSideMenu, shot } from "./lib/ui";

const POLL_URL = `${API_ORIGIN}/v1/local-bridge/poll`;
const WRONG_TOKEN = "e2e-not-the-real-pairing-code";

async function openLocalHelperTab(page: Page): Promise<void> {
  await openSideMenu(page);
  await page.getByRole("menuitem", { name: "設定", exact: true }).click();
  await page.getByText("代理人能力", { exact: true }).first().click();
  await page.getByText("本機小幫手", { exact: true }).first().click();
  await page.getByRole("heading", { name: "本機小幫手" }).first().waitFor();
}

function pollAsHelper(request: APIRequestContext, token: string) {
  return request.post(POLL_URL, {
    headers: { "X-Touka-Local-Token": token },
    data: { protocol: 1, helper_version: "e2e" },
  });
}

test("設定頁配對：碼只顯示一次、小幫手連上看得到、解除後舊碼失效", async ({ page, request }, info) => {
  await loginInBrowser(page);
  await openLocalHelperTab(page);
  await expect(page.getByText("還沒有配對")).toBeVisible();
  await shot(page, info, "unpaired");

  await page.getByRole("button", { name: "產生配對碼" }).click();
  const title = page.getByText("配對碼（只顯示這一次）");
  await expect(title).toBeVisible();
  const code = (await title.locator("..").locator("code").textContent()) ?? "";
  expect(code.length).toBeGreaterThan(20);
  await expect(page.getByText("已配對，小幫手沒有開著")).toBeVisible();
  await shot(page, info, "paired");

  expect((await pollAsHelper(request, WRONG_TOKEN)).status()).toBe(401);
  expect((await pollAsHelper(request, code)).status()).toBe(200);
  await expect(page.getByText("已連線（小幫手版本 e2e）")).toBeVisible({ timeout: 15_000 });
  await shot(page, info, "online");

  await page.reload();
  await openLocalHelperTab(page);
  await expect(page.getByText("已連線（小幫手版本 e2e）")).toBeVisible();
  await expect(page.getByText("配對碼（只顯示這一次）")).toHaveCount(0);

  await page.getByRole("button", { name: "解除配對" }).click();
  await page.getByRole("button", { name: "確定", exact: true }).click();
  await expect(page.getByText("還沒有配對")).toBeVisible();
  expect((await pollAsHelper(request, code)).status()).toBe(401);
});
