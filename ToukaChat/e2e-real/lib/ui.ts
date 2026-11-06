import { expect, type Locator, type Page, type TestInfo } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { ENV_EMAIL, ENV_PASSWORD, SHOTS_DIR, SOUL_ID } from "./constants";

export interface HttpHookSpec {
  url: string;
  name: string;
  matcher?: string;
  headers?: string;
}

/**
 * 留給審查員與人核對的截圖：檔名是「測試標題-步驟」。設定頁是內層捲動，fullPage 照不到下面，
 * 要核對的東西不在第一屏時傳 focus，先捲過去；提示訊息的淡入也先快轉完。
 */
export async function shot(page: Page, info: TestInfo, step: string, focus?: Locator): Promise<void> {
  fs.mkdirSync(SHOTS_DIR, { recursive: true });
  const title = info.title.replace(/[^\p{L}\p{N}]+/gu, "_").slice(0, 60);
  await focus?.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(SHOTS_DIR, `${title}-${step}.png`), animations: "disabled" });
}

export function composer(page: Page) {
  return page.getByPlaceholder("在此輸入指令或對話...");
}

export async function loginInBrowser(
  page: Page,
  email: string = process.env[ENV_EMAIL] ?? "",
  password: string = process.env[ENV_PASSWORD] ?? "",
): Promise<void> {
  await page.goto("/login");
  await submitLogin(page, email, password);
}

/** 已經在登入頁時直接填：不重新載入頁面，才驗得到同一頁換帳號。 */
export async function submitLogin(page: Page, email: string, password: string): Promise<void> {
  await page.getByPlaceholder("Email").fill(email);
  await page.getByPlaceholder("Password").fill(password);
  await page.keyboard.press("Enter");
  await page.waitForURL((url) => !url.pathname.includes("login"));
  await composer(page).waitFor();
}

export async function openChat(page: Page): Promise<void> {
  await page.goto("/?view=chat");
  await composer(page).waitFor();
}

export async function selectSoul(page: Page, soulId: string = SOUL_ID): Promise<void> {
  const picker = page.getByRole("combobox", { name: "選擇模型" });
  await picker.click();
  await page.getByRole("option", { name: soulId }).click();
  await expect(picker).toContainText(soulId);
}

/** 用 Enter 送：換模型的提示可能還蓋在「傳送」鈕上，點按鈕要等提示消失。 */
export async function sendMessage(page: Page, text: string): Promise<void> {
  await composer(page).fill(text);
  await composer(page).press("Enter");
}

/** 沒有保存提示：沒有通知、沒有「重新核對保存結果」按鈕，也沒有保存的狀態列。 */
export async function expectNoSaveNotice(page: Page): Promise<void> {
  await expect(page.getByText("儲存工作階段失敗")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "重新核對保存結果" })).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: /保存/ })).toHaveCount(0);
}

/** 最後一則回覆有失敗標示。訊息清單是虛擬捲動，捲出畫面的不在頁面上，不能數總數。 */
export async function expectLastReplyMarkedFailed(page: Page): Promise<void> {
  const lastItem = page.locator("[data-index]").last();
  await expect(lastItem.getByRole("alert")).toHaveText(/這一輪回覆失敗/);
}

function sessionOptions(page: Page, title?: string): Locator {
  if (title) return page.getByRole("button", { name: `會話選項：${title}` }).first();
  return page.locator('aside [role="treeitem"][aria-current="true"]').getByRole("button", { name: /^會話選項：/ });
}

/** 沒給標題時是開著的這段。 */
export async function setChatAsHome(page: Page, title?: string): Promise<void> {
  await sessionOptions(page, title).click();
  await page.getByRole("menuitem", { name: "設為家對話" }).click();
  await page.keyboard.press("Escape");
}

export async function unsetChatHome(page: Page, title: string): Promise<void> {
  await sessionOptions(page, title).click();
  await page.getByRole("menuitem", { name: "取消家對話" }).click();
  await page.keyboard.press("Escape");
}

/** 清單上這段是不是家：選單給的動作跟著畫面記的家標記走，家對話就是資料夾那一列。 */
export async function expectChatIsHome(page: Page, title: string, isHome: boolean): Promise<void> {
  await expect(page.getByRole("treeitem", { name: new RegExp(`，家對話：${title}`) })).toHaveCount(isHome ? 1 : 0);
  await sessionOptions(page, title).click();
  // 先等該有的動作出現：選單還沒打開時，「沒有」的檢查會直接過
  const [shown, hidden] = isHome ? ["取消家對話", "設為家對話"] : ["設為家對話", "取消家對話"];
  await expect(page.getByRole("menuitem", { name: shown })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: hidden })).toHaveCount(0);
  await page.keyboard.press("Escape");
}

/** 點清單上的這段（不能是家對話，那一列顯示的是靈魂名）。訊息可能是同樣的文字，要限定在清單裡。 */
export async function clickChatInList(page: Page, title: string): Promise<void> {
  await page.locator('aside [role="treeitem"]').getByText(title, { exact: true }).first().click();
}

export async function renameChat(page: Page, title: string, next: string): Promise<void> {
  await sessionOptions(page, title).click();
  await page.getByRole("menuitem", { name: "重命名" }).click();
  // 輸入框一打開就有焦點；用原標題找的話，填完就找不到了
  await page.locator(`input[value="${title}"]`).fill(next);
  await page.keyboard.press("Enter");
}

/** 左側邊欄底部的選單按鈕：其他畫面與登出都收在這裡。 */
export function sideMenuButton(page: Page): Locator {
  return page.getByRole("button", { name: /^選單：/ });
}

export async function openSideMenu(page: Page): Promise<void> {
  await sideMenuButton(page).click();
  await page.getByRole("menu").waitFor();
}

export async function openHooksTab(page: Page): Promise<void> {
  await openSideMenu(page);
  await page.getByRole("menuitem", { name: "設定", exact: true }).click();
  await page.getByText("系統管理", { exact: true }).first().click();
  await page.getByText("Hooks", { exact: true }).first().click();
  await page.getByText("啟用 Hooks").waitFor();
}

export function eventSection(page: Page, event: string) {
  return page.locator("section").filter({ has: page.getByRole("heading", { name: event, exact: true }) });
}

export async function enableHooks(page: Page): Promise<void> {
  await page.getByRole("switch", { name: "啟用 Hooks" }).click();
}

/** 走設定頁新增一個 http hook：新增一組預設帶一個程式 hook，換成 HTTP。同一事件可以加好幾組。 */
export async function addHttpHook(page: Page, event: string, spec: HttpHookSpec): Promise<void> {
  const section = eventSection(page, event);
  await section.getByRole("button", { name: "新增一組" }).click();
  const newGroup = section.locator(":scope > div").last();
  await newGroup.getByRole("button", { name: "HTTP", exact: true }).click();
  await newGroup.getByRole("button", { name: "刪除這個 hook" }).first().click();
  await newGroup.getByRole("textbox", { name: "名稱（選填）" }).fill(spec.name);
  await newGroup.getByRole("textbox", { name: "網址" }).fill(spec.url);
  if (spec.headers) await newGroup.getByRole("textbox", { name: "標頭" }).fill(spec.headers);
  if (spec.matcher) await newGroup.getByRole("textbox", { name: "比對規則" }).fill(spec.matcher);
}

export async function saveHooks(page: Page): Promise<void> {
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await page.getByText("Hook 設定已儲存").first().waitFor();
}
