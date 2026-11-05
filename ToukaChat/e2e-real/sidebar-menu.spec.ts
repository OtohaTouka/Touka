import { expect, test, type Page } from "@playwright/test";
import { adminLogin, savedMessages, sessionIdByTitle } from "./lib/api";
import { FAKE_TEXT_REPLY } from "./lib/constants";
import { modelCalls } from "./lib/control";
import { composer, loginInBrowser, openSideMenu, sendMessage, shot, sideMenuButton } from "./lib/ui";

// 沙盒是整合模式：非整合模式少三項，由 NavigationArea 的單元測試驗。
const PAGES = ["專案管理", "靈魂核心", "模型工坊", "系統脈搏", "記憶寶庫", "設定"];
// 按鈕上的標示只證明狀態換了，標題才證明畫面內容真的換了。
// 靈魂核心開清單第一個靈魂，順序看檔案系統（Windows 照字母、Linux 不一定），不能比靈魂名
const PAGE_HEADINGS: Record<string, string> = {
  專案管理: "專案管理",
  靈魂核心: "燈火靈魂",
  模型工坊: "工具權限",
  系統脈搏: "脈搏控制中心",
  記憶寶庫: "記憶寶庫",
  設定: "一般設定",
};

let adminToken = "";

async function expectOnPage(page: Page, label: string): Promise<void> {
  await expect(sideMenuButton(page)).toHaveAccessibleName(new RegExp(`目前在${label}$`));
  await expect(sideMenuButton(page)).toContainText(label);
}

async function expectInChat(page: Page): Promise<void> {
  await composer(page).waitFor();
  await expect(sideMenuButton(page)).not.toHaveAccessibleName(/目前在/);
}

test.beforeAll(async () => {
  adminToken = await adminLogin();
});

test.beforeEach(async ({ page }) => {
  await loginInBrowser(page);
});

test("底部選單：其他選項都收進去，鍵盤打得開也關得掉", async ({ page }, info) => {
  await expect(page.getByRole("button", { name: "設定", exact: true })).toHaveCount(0);
  await expect(page.getByText("聊天大廳")).toHaveCount(0);

  await sideMenuButton(page).focus();
  await page.keyboard.press("Enter");
  const items = page.getByRole("menuitem");
  await expect(items).toHaveText([...PAGES, "登出"]);
  await expect(items.first()).toBeFocused();
  await shot(page, info, "open");

  await page.keyboard.press("Escape");
  await expect(items).toHaveCount(0);
  await expect(sideMenuButton(page)).toBeFocused();
});

test("每個項目都到得了；按鈕標出目前畫面；點對話或新對話回到聊天", async ({ page }, info) => {
  await composer(page).fill("選單實際驗證");
  await composer(page).press("Enter");
  await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
  const newChat = page.getByRole("button", { name: "新增對話" });
  const incognito = page.getByRole("button", { name: "無痕對話" });
  const [newBox, incognitoBox] = [await newChat.boundingBox(), await incognito.boundingBox()];
  expect(newBox?.y).toBe(incognitoBox?.y);

  for (const label of PAGES) {
    await openSideMenu(page);
    await page.getByRole("menuitem", { name: label, exact: true }).click();
    await expect(page.getByRole("menu")).toHaveCount(0);
    await expectOnPage(page, label);
    await expect(page.getByRole("heading", { name: PAGE_HEADINGS[label] }).first()).toBeVisible();
    await shot(page, info, `page-${label}`);
    await openSideMenu(page);
    await expect(page.getByRole("menuitem", { name: label, exact: true })).toHaveAttribute("aria-current", "page");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu")).toHaveCount(0);
  }
  await shot(page, info, "settings-marked");

  // 迴圈最後停在設定：點目前的對話回聊天，按鈕上的標示要消失
  await page.locator('aside [role="treeitem"][aria-current="true"]').click();
  await expectInChat(page);
  await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
  await shot(page, info, "back-to-chat");

  // 回聊天不能動到存檔要用的版本：再送一則，模型要收到、存檔要有第二輪、不跳保存提示
  await sendMessage(page, "回到聊天後再送一則");
  await expect.poll(async () => JSON.stringify(await modelCalls())).toContain("回到聊天後再送一則");
  const sessionId = await sessionIdByTitle(adminToken, "選單實際驗證");
  await expect
    .poll(async () => (await savedMessages(adminToken, sessionId)).filter((message) => message.role === "assistant").length)
    .toBe(2);
  await expect(page.getByRole("button", { name: "重新核對保存結果" })).toHaveCount(0);

  await openSideMenu(page);
  await page.getByRole("menuitem", { name: "設定", exact: true }).click();
  await expectOnPage(page, "設定");
  await newChat.click();
  await expectInChat(page);
});

test("登出：第一下不關選單；關掉再開確認就清掉；按兩下才登出", async ({ page }, info) => {
  await openSideMenu(page);
  await page.getByRole("menuitem", { name: "登出", exact: true }).click();
  const confirm = page.getByRole("menuitem", { name: "確認登出？", exact: true });
  await expect(confirm).toBeVisible();
  await shot(page, info, "confirm");
  expect(page.url()).not.toContain("login");

  // 確認字樣一關就換回來；要等選單的收合動畫跑完才重開
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await openSideMenu(page);
  await expect(page.getByRole("menuitem", { name: "登出", exact: true })).toBeVisible();
  await expect(confirm).toHaveCount(0);

  await page.getByRole("menuitem", { name: "登出", exact: true }).click();
  await confirm.click();
  await page.waitForURL((url) => url.pathname.includes("login"));
});

test.describe("手機", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true });

  test("頂列沒有設定鈕；選單選了之後抽屜關掉，畫面還點得動", async ({ page }, info) => {
    // 手機第一次進來抽屜是開的（既有行為），後面的頂列被它蓋住
    const drawer = page.getByRole("dialog");
    await expect(drawer).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);

    // 頂列在 main 裡面，不算 banner 地標
    const header = page.locator("header");
    // 剩開側欄與工具面板兩顆
    await expect(header.getByRole("button")).toHaveCount(2);
    await shot(page, info, "header");

    await header.getByRole("button").first().tap();
    await expect(drawer).toBeVisible();
    await sideMenuButton(page).tap();
    await expect(page.getByRole("menuitem")).toHaveText([...PAGES, "登出"]);
    await shot(page, info, "menu");

    await page.getByRole("menuitem", { name: "設定", exact: true }).tap();
    await expect(drawer).toHaveCount(0);
    await page.getByRole("heading", { name: "一般設定" }).waitFor();
    await shot(page, info, "settings");
    expect(await page.evaluate(() => getComputedStyle(document.body).pointerEvents)).not.toBe("none");

    // 換一顆按鈕真的點下去，確認畫面沒有卡住
    await header.getByRole("button").last().tap();
    await expect(drawer).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);

    // 窄 IDE 面板有鍵盤：選單開著按 Esc 只關選單，抽屜留著
    await header.getByRole("button").first().tap();
    await expect(drawer).toBeVisible();
    await sideMenuButton(page).tap();
    await page.getByRole("menu").waitFor();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu")).toHaveCount(0);
    await expect(drawer).toBeVisible();

    // 回聊天：抽屜要關掉，不能留在設定頁
    await page.getByRole("button", { name: "新增對話" }).tap();
    await expect(drawer).toHaveCount(0);
    await composer(page).waitFor();
    await expect(page.getByRole("heading", { name: "一般設定" })).toHaveCount(0);
  });
});
