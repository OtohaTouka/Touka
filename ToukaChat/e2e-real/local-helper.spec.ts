import { createHash, createHmac } from "node:crypto";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { API_ORIGIN } from "./lib/constants";
import { loginInBrowser, openSideMenu, shot } from "./lib/ui";

const POLL_URL = `${API_ORIGIN}/v1/local-bridge/poll`;
const WRONG_TOKEN = "e2e-not-the-real-pairing-code";
// 協議第 2 版（ADR-0317）：配對碼是「權杖.鑰匙」，小幫手只拿權杖輪詢，鑰匙留著驗章
const PROTOCOL = 2;

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
    data: { protocol: PROTOCOL, helper_version: "e2e" },
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
  const [token, key] = code.split(".");
  expect(token.length).toBeGreaterThan(20);
  expect(key).toHaveLength(43);
  await expect(page.getByText("已配對，小幫手沒有開著")).toBeVisible();
  await shot(page, info, "paired");

  expect((await pollAsHelper(request, WRONG_TOKEN)).status()).toBe(401);
  expect((await pollAsHelper(request, code)).status()).toBe(401);
  expect((await pollAsHelper(request, token)).status()).toBe(200);
  await expect(page.getByText("已連線（小幫手版本 e2e）")).toBeVisible({ timeout: 15_000 });
  await shot(page, info, "online");

  await page.reload();
  await openLocalHelperTab(page);
  await expect(page.getByText("已連線（小幫手版本 e2e）")).toBeVisible();
  await expect(page.getByText("配對碼（只顯示這一次）")).toHaveCount(0);

  await page.getByRole("button", { name: "解除配對" }).click();
  await page.getByRole("button", { name: "確定", exact: true }).click();
  await expect(page.getByText("還沒有配對")).toBeVisible();
  expect((await pollAsHelper(request, token)).status()).toBe(401);
});

// ADR-0320：從設定頁請小幫手開放資料夾。測試扮小幫手，用配對碼裡的鑰匙驗 Touka 蓋的章。
const RESULT_URL = (jobId: string) => `${API_ORIGIN}/v1/local-bridge/jobs/${jobId}/result`;
// 路徑不以磁碟代號開頭：Touka 不判斷路徑，規則都在小幫手
const OPEN_PATH = "E2E/相片";

interface OpenJob {
  id: string;
  action: string;
  args: Record<string, unknown>;
  decide_within: number;
  decision: { nonce: string; digest: string; shown: string; decided_by: string; signature: string };
}

function fingerprint(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

/** 和 Touka、小幫手相同的蓋章算法：排序後的 JSON（欄位都是 ASCII）做 HMAC-SHA256。 */
function openStamp(key: Buffer, jobId: string, nonce: string, path: string): string {
  const print = fingerprint(path);
  const message = JSON.stringify({
    act: "request_open",
    by: "owner_request",
    digest: print,
    job: jobId,
    nonce,
    shown: print,
    v: 2,
  });
  return createHmac("sha256", key).update(message, "ascii").digest("hex");
}

async function pairAndConnect(page: Page, request: APIRequestContext): Promise<{ token: string; key: Buffer }> {
  await loginInBrowser(page);
  await openLocalHelperTab(page);
  const pair = page.getByRole("button", { name: "產生配對碼" });
  if (await pair.isVisible()) {
    await pair.click();
  } else {
    await page.getByRole("button", { name: "換一組配對碼" }).click();
    await page.getByRole("button", { name: "確定", exact: true }).click();
  }
  const title = page.getByText("配對碼（只顯示這一次）");
  await expect(title).toBeVisible();
  const code = (await title.locator("..").locator("code").textContent()) ?? "";
  const [token, keyText] = code.split(".");
  expect((await pollAsHelper(request, token)).status()).toBe(200);
  await expect(page.getByText("已連線（小幫手版本 e2e）")).toBeVisible({ timeout: 15_000 });
  return { token, key: Buffer.from(keyText, "base64url") };
}

async function nextJob(request: APIRequestContext, token: string): Promise<OpenJob> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const body = await (await pollAsHelper(request, token)).json();
    if (body.job) return body.job as OpenJob;
  }
  throw new Error("no job reached the helper");
}

async function askToOpen(page: Page): Promise<void> {
  await page.getByLabel("資料夾路徑").fill(`"${OPEN_PATH}"`);
  await page.getByRole("button", { name: "請小幫手開放" }).click();
  await expect(page.getByText("請到小幫手的視窗查看並決定（2 分鐘內）。")).toBeVisible();
}

test("設定頁請小幫手開放資料夾：帶配對鑰匙的章，核准後顯示已開放", async ({ page, request }, info) => {
  const { token, key } = await pairAndConnect(page, request);
  await expect(page.getByText("請小幫手開放資料夾")).toBeVisible();
  await askToOpen(page);
  await shot(page, info, "open-waiting");

  const job = await nextJob(request, token);
  expect(job.action).toBe("request_open");
  expect(job.args).toEqual({ path: OPEN_PATH });
  expect(job.decide_within).toBe(120);
  expect(job.decision.decided_by).toBe("owner_request");
  expect(job.decision.digest).toBe(fingerprint(OPEN_PATH));
  expect(job.decision.shown).toBe(fingerprint(OPEN_PATH));
  expect(job.decision.signature).toBe(openStamp(key, job.id, job.decision.nonce, OPEN_PATH));

  const answer = await request.post(RESULT_URL(job.id), {
    headers: { "X-Touka-Local-Token": token },
    data: { status: "ok", data: { name: "相片", writable: false } },
  });
  expect(answer.status()).toBe(200);
  await expect(page.getByText("已開放「相片」（只能看）。")).toBeVisible({ timeout: 15_000 });
  await shot(page, info, "open-done");
});

test("設定頁請小幫手開放資料夾：小幫手沒有開放時照實顯示", async ({ page, request }, info) => {
  const { token } = await pairAndConnect(page, request);
  await askToOpen(page);
  const job = await nextJob(request, token);
  const answer = await request.post(RESULT_URL(job.id), {
    headers: { "X-Touka-Local-Token": token },
    data: { status: "refused", code: "not_opened", message: "The user did not open that folder." },
  });
  expect(answer.status()).toBe(200);
  await expect(page.getByText("小幫手沒有開放這個資料夾，原因寫在小幫手的視窗。")).toBeVisible({ timeout: 15_000 });
  await shot(page, info, "open-refused");
});
