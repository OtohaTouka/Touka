import { expect, type Locator, type Page, type Response } from "@playwright/test";
import { createChat, createSoul, createUser, login, type ChatSeed } from "./api";
import { MODEL_HOLD_REQUEST } from "./constants";
import { modelHolding } from "./control";
import { sendMessage } from "./ui";

// 每個情境用新的靈魂與帳號：心跳對話編號依靈魂固定，共用的靈魂新對話出現會假綠。
export const PASSWORD = "session-activity-password-1";
const SEED_EPOCH_MS = 1_790_000_000_000;
const ACTIVITY_URL = "**/v1/ui/activity*";
export const SNAPSHOT_URL = /\/v1\/ui\/sessions\/[^/?]+\?snapshot=true/;

/** 名稱要各情境不同：測試失敗後 worker 會重開，計數器不可靠。 */
export async function newSoulAccount(adminToken: string, name: string, souls: string[]): Promise<{ email: string; id: string; token: string }> {
  for (const soul of souls) await createSoul(adminToken, soul);
  const email = `activity-${name}@touka.io`;
  const id = await createUser(adminToken, email, PASSWORD, souls);
  return { email, id, token: await login(email, PASSWORD) };
}

/** updatedAt 寫相對值，截圖上才不會是 1970 年。 */
export async function seedChats(token: string, chats: ChatSeed[]): Promise<void> {
  for (const chat of chats) await createChat(token, { ...chat, updatedAt: SEED_EPOCH_MS + (chat.updatedAt ?? 0) * 1000 });
}

/** 列的無障礙名稱：名稱，後面接狀態（「甲，回覆中」）。彈窗開著時頁面其他部分是 aria-hidden，清單還是要找得到。 */
export function row(page: Page, name: string | RegExp): Locator {
  return page.getByRole("treeitem", { name, exact: typeof name === "string", includeHidden: true });
}

export function rowNames(page: Page): Promise<(string | null)[]> {
  return page.getByRole("treeitem").evaluateAll((items) => items.map((item) => item.getAttribute("aria-label") ?? item.textContent));
}

export const pulseTitle = (soul: string) => `Pulse: ${soul}`;

/** 送一則，模型收到了但先不回；放行用 releaseModel。 */
export async function sendHeld(page: Page): Promise<void> {
  await sendMessage(page, MODEL_HOLD_REQUEST);
  await modelHolding();
}

/** 下一次查詢工作狀態的回應。 */
export function activityPolled(page: Page): Promise<Response> {
  return page.waitForResponse((response) => new URL(response.url()).pathname === "/v1/ui/activity");
}

/** 這個元素正在跑的呼吸動畫的週期。截圖會把動畫停掉，要直接問瀏覽器。 */
function breathingPeriods(target: Locator): Promise<number[]> {
  return target.evaluate((element) =>
    element
      .getAnimations()
      .filter((animation) => animation instanceof CSSAnimation && animation.animationName === "touka-breathe")
      .map((animation) => Number(animation.effect?.getComputedTiming().duration)),
  );
}

export async function expectBreathing(target: Locator): Promise<void> {
  await expect.poll(() => breathingPeriods(target)).toHaveLength(1);
  const [period] = await breathingPeriods(target);
  expect(period).toBeGreaterThanOrEqual(2000);
}

export async function expectNotBreathing(target: Locator): Promise<void> {
  expect(await breathingPeriods(target)).toEqual([]);
}

/** 整列（含子元素）有沒有呼吸動畫。 */
export function breathingAnywhere(target: Locator): Promise<number> {
  return target.evaluate((element) =>
    element
      .getAnimations({ subtree: true })
      .filter((animation) => animation instanceof CSSAnimation && animation.animationName === "touka-breathe").length,
  );
}

/** 先把查詢扣住，放行後才到後端：模擬「兩次查詢之間發生的事」。回傳放行的函式；放行後攔截還在，只是直接過。 */
export async function pauseActivity(page: Page): Promise<() => void> {
  let resume!: () => void;
  const gate = new Promise<void>((resolve) => {
    resume = resolve;
  });
  await page.route(ACTIVITY_URL, async (route) => {
    await gate;
    await route.continue();
  });
  return resume;
}

/** 查詢已經拿到後端的回應，但晚一點才交給畫面。回傳 `fetched`（拿到了）與放行的函式。 */
export async function delayActivityResponse(page: Page): Promise<{ fetched: Promise<void>; deliver: () => void }> {
  let markFetched!: () => void;
  const fetched = new Promise<void>((resolve) => {
    markFetched = resolve;
  });
  let deliver!: () => void;
  const gate = new Promise<void>((resolve) => {
    deliver = resolve;
  });
  await page.route(ACTIVITY_URL, async (route) => {
    const response = await route.fetch();
    markFetched();
    await gate;
    await route.fulfill({ response });
  });
  return { fetched, deliver };
}

/** 查詢一律失敗；回傳恢復的函式。 */
export async function failActivity(page: Page): Promise<() => Promise<void>> {
  await page.route(ACTIVITY_URL, (route) => route.fulfill({ status: 500, json: {} }));
  return () => page.unroute(ACTIVITY_URL);
}

/** 記下之後所有抓整份清單的請求。 */
export function recordListRequests(page: Page): string[] {
  const seen: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "GET" && /\/v1\/ui\/sessions\/?$/.test(new URL(request.url()).pathname)) seen.push(request.url());
  });
  return seen;
}

/** 記下之後查工作狀態的次數。 */
export function countActivityRequests(page: Page): { count: number } {
  const counter = { count: 0 };
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/v1/ui/activity") counter.count += 1;
  });
  return counter;
}

/** 頁面以為分頁被藏起來或回來了（無頭瀏覽器不會自己切）。 */
export async function setTabVisibility(page: Page, state: "hidden" | "visible"): Promise<void> {
  await page.evaluate((next) => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => next });
    document.dispatchEvent(new Event("visibilitychange"));
  }, state);
}

export async function logOut(page: Page): Promise<void> {
  await page.getByRole("button", { name: /^選單：/ }).click();
  await page.getByRole("menuitem", { name: "登出", exact: true }).click();
  await page.getByRole("menuitem", { name: "確認登出？", exact: true }).click();
  await page.waitForURL((url) => url.pathname.includes("login"));
}

export async function deleteChatInList(page: Page, title: string): Promise<void> {
  await page.getByRole("button", { name: `會話選項：${title}` }).click();
  await page.getByRole("menuitem", { name: "刪除對話" }).click();
  await page.getByRole("menuitem", { name: /確認刪除/ }).click();
}

export function savedHints(page: Page): Locator {
  return page.getByRole("status").filter({ hasText: /保存/ });
}
