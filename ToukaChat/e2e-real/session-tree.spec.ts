import { expect, test, type Locator, type Page } from "@playwright/test";
import { adminLogin, createChat, createProject, createSoul, createUser, login, setChatHome, type ChatSeed } from "./lib/api";
import { FAKE_MODEL_ID, FAKE_TEXT_REPLY } from "./lib/constants";
import { modelCalls } from "./lib/control";
import { clickChatInList, composer, loginInBrowser, openSideMenu, selectSoul, sendMessage, setChatAsHome, shot, sideMenuButton, submitLogin } from "./lib/ui";

// 每個情境用新的靈魂與帳號：共用沙盒裡 hooks-* 的靈魂已經有對話和家，一般帳號只看得到自己的對話。
const PASSWORD = "session-tree-password-1";
const COLLAPSED_KEY = "touka_session_tree_collapsed:";
const SEED_EPOCH_MS = 1_790_000_000_000;

let adminToken = "";

/** 名稱要各情境不同：測試失敗後 worker 會重開，計數器不可靠。 */
async function newAccount(name: string, souls: string[]): Promise<{ email: string; token: string }> {
  const email = `tree-${name}@touka.io`;
  await createUser(adminToken, email, PASSWORD, souls);
  return { email, token: await login(email, PASSWORD) };
}

async function newSoulAccount(name: string, souls: string[]): Promise<{ email: string; token: string }> {
  for (const soul of souls) await createSoul(adminToken, soul);
  return newAccount(name, souls);
}

/** updatedAt 寫相對值，截圖上才不會是 1970 年。 */
async function seed(token: string, chats: ChatSeed[]): Promise<void> {
  for (const chat of chats) await createChat(token, { ...chat, updatedAt: SEED_EPOCH_MS + (chat.updatedAt ?? 0) * 1000 });
}

function row(page: Page, name: string | RegExp): Locator {
  return page.getByRole("treeitem", { name, exact: typeof name === "string" });
}

function rowNames(page: Page): Promise<(string | null)[]> {
  return page.getByRole("treeitem").evaluateAll((items) => items.map((item) => item.getAttribute("aria-label") ?? item.textContent));
}

const arrow = (target: Locator) => target.getByTestId("session-tree-toggle");
const treeSkeleton = (page: Page) => page.getByRole("status", { name: "對話清單載入中" });

test.beforeAll(async () => {
  adminToken = await adminLogin();
});

test("一個靈魂有家和 7 個其他對話：資料夾是靈魂名、家在最上面、先列 5 筆、開著的不收起來", async ({ page }, info) => {
  const soul = "TreeFive";
  const { email, token } = await newSoulAccount("five", [soul]);
  await seed(token, [
    { id: "five-home", title: "五的家", agentId: soul, updatedAt: 100 },
    ...[1, 2, 3, 4, 5, 6, 7].map((n) => ({ id: `five-${n}`, title: `五之${n}`, agentId: soul, updatedAt: 100 + n })),
  ]);
  await setChatHome(token, "five-home", soul);
  await loginInBrowser(page, email, PASSWORD);

  const home = `${soul}，家對話：五的家`;
  await expect(row(page, home)).toHaveAttribute("aria-expanded", "true");
  await expect(row(page, home)).toHaveAttribute("aria-level", "1");
  await expect(row(page, "五之7")).toHaveAttribute("aria-level", "2");
  const names = await rowNames(page);
  expect(names.slice(names.indexOf(home))).toEqual([home, "五之7", "五之6", "五之5", "五之4", "五之3", "還有 2 個"]);
  await expect(row(page, home).getByTitle("五的家")).toHaveText(soul);
  await shot(page, info, "folder");

  await row(page, "還有 2 個").click();
  await row(page, "五之1").click();
  await expect(row(page, "五之1")).toHaveAttribute("aria-current", "true");

  await page.reload();
  await expect(row(page, "五之1")).toHaveAttribute("aria-current", "true");
  await expect(row(page, "還有 1 個")).toBeVisible();
  await expect(row(page, "五之2")).toHaveCount(0);
  await shot(page, info, "open-stays");
});

test("資料夾那一列：點名字開家對話、點箭頭收合展開、沒有家的只展開、「⋯」是家對話的選單", async ({ page }, info) => {
  const [withHome, noHome] = ["TreeSixA", "TreeSixB"];
  const { email, token } = await newSoulAccount("six", [withHome, noHome]);
  await seed(token, [
    { id: "six-home", title: "六的家", agentId: withHome, updatedAt: 30 },
    { id: "six-child", title: "六之一", agentId: withHome, updatedAt: 20 },
    { id: "six-b", title: "六之二", agentId: noHome, updatedAt: 10 },
  ]);
  await setChatHome(token, "six-home", withHome);
  await loginInBrowser(page, email, PASSWORD);

  const folder = row(page, new RegExp(`^${withHome}，家對話：六的家`));
  await folder.getByText(withHome).click();
  await expect(folder).toHaveAttribute("aria-current", "true");
  await expect(folder).toHaveAttribute("aria-expanded", "true");

  await row(page, "六之一").click();
  await expect(row(page, "六之一")).toHaveAttribute("aria-current", "true");
  await arrow(folder).click();
  await expect(folder).toHaveAttribute("aria-expanded", "false");
  await expect(row(page, "六之一")).toHaveCount(0);
  // 開著的對話收進去了：資料夾那一列亮起來，畫面上才找得到它
  await expect(folder).toHaveAttribute("aria-current", "true");
  await expect(folder).toHaveAttribute("aria-label", /開著的對話在裡面$/);
  await shot(page, info, "collapsed-holds-active");
  await arrow(folder).click();
  await expect(row(page, "六之一")).toHaveAttribute("aria-current", "true");
  await expect(folder).not.toHaveAttribute("aria-current", "true");

  const bare = row(page, noHome);
  await arrow(bare).click();
  await expect(bare).toHaveAttribute("aria-expanded", "false");
  await bare.getByText(noHome).click();
  await expect(bare).toHaveAttribute("aria-expanded", "true");
  await bare.getByText(noHome).click();
  await expect(bare).toHaveAttribute("aria-expanded", "true");
  await expect(row(page, "六之一")).toHaveAttribute("aria-current", "true");
  await expect(bare.getByRole("button")).toHaveCount(0);

  await folder.getByRole("button", { name: "會話選項：六的家" }).click();
  await expect(page.getByRole("menuitem", { name: "取消家對話" })).toBeVisible();
  await shot(page, info, "folder-menu");
  await page.keyboard.press("Escape");
});

test("收合後重新整理還在；換一個帳號不受影響、沒有殘留", async ({ page }, info) => {
  const soul = "TreeSeven";
  const first = await newSoulAccount("seven-first", [soul]);
  const second = await newAccount("seven-second", [soul]);
  await seed(first.token, [{ id: "seven-first", title: "七之甲", agentId: soul, updatedAt: 10 }]);
  await seed(second.token, [{ id: "seven-second", title: "七之乙", agentId: soul, updatedAt: 10 }]);

  await loginInBrowser(page, first.email, PASSWORD);
  await arrow(row(page, soul)).click();
  await expect(row(page, soul)).toHaveAttribute("aria-expanded", "false");
  await page.reload();
  await expect(row(page, soul)).toHaveAttribute("aria-expanded", "false");
  await shot(page, info, "collapsed-after-reload");

  await openSideMenu(page);
  await page.getByRole("menuitem", { name: "登出", exact: true }).click();
  await page.getByRole("menuitem", { name: "確認登出？", exact: true }).click();
  await page.waitForURL((url) => url.pathname.includes("login"));

  // 同一頁換帳號：登出、登入都不重新載入，頁面上的狀態要自己換掉
  await submitLogin(page, second.email, PASSWORD);
  await expect(row(page, soul)).toHaveAttribute("aria-expanded", "true");
  await expect(row(page, "七之乙")).toBeVisible();

  await openSideMenu(page);
  await page.getByRole("menuitem", { name: "登出", exact: true }).click();
  await page.getByRole("menuitem", { name: "確認登出？", exact: true }).click();
  await page.waitForURL((url) => url.pathname.includes("login"));

  await submitLogin(page, first.email, PASSWORD);
  await expect(row(page, soul)).toHaveAttribute("aria-expanded", "false");
});

test("沒有靈魂的對話放其他對話並標模型；冒號格式進對應資料夾；只有無痕的資料夾不寫瀏覽器", async ({ page }, info) => {
  const [soul, secret] = ["TreeEight", "TreeEightSecret"];
  const { email, token } = await newSoulAccount("eight", [soul, secret]);
  await seed(token, [
    { id: "eight-colon", title: "八之冒號", agentId: `toukasoul:${soul}`, updatedAt: 30 },
    { id: "eight-plain", title: "八之一", agentId: soul, updatedAt: 20 },
    { id: "eight-model", title: "八之模型", agentId: FAKE_MODEL_ID, updatedAt: 10 },
  ]);
  await loginInBrowser(page, email, PASSWORD);

  await expect(row(page, soul)).toBeVisible();
  await expect(row(page, "八之冒號")).toHaveAttribute("aria-level", "2");
  await expect(row(page, "八之一")).toHaveAttribute("aria-level", "2");
  const others = page.getByRole("group", { name: "其他對話" });
  await expect(others.getByRole("treeitem", { name: `八之模型，${FAKE_MODEL_ID}` })).toHaveAttribute("aria-level", "1");
  await shot(page, info, "others");

  // 無痕對話第一次發言才決定靈魂，在那之前放其他對話
  await page.getByRole("button", { name: "無痕對話" }).click();
  await expect(others.getByRole("treeitem", { name: /無痕對話$/ })).toBeVisible();
  await selectSoul(page, secret);
  await sendMessage(page, "無痕的一則");
  await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
  const secretFolder = row(page, new RegExp(`^${secret}`));
  await expect(secretFolder).toBeVisible();
  await expect(page.getByRole("treeitem", { name: /無痕對話$/ })).toHaveAttribute("aria-level", "2");
  await shot(page, info, "incognito");
  await arrow(secretFolder).click();
  await expect(secretFolder).toHaveAttribute("aria-expanded", "false");
  await expect(secretFolder).toHaveAttribute("aria-label", /開著的對話在裡面$/);
  // eslint-disable-next-line no-restricted-globals -- 測試要看瀏覽器實際存了什麼，不能經 Hook
  const stored = await page.evaluate((prefix) => Object.entries(localStorage).filter(([key]) => key.startsWith(prefix)).map(([, value]) => value), COLLAPSED_KEY);
  expect(stored.join("")).not.toContain(secret.toLowerCase());
});

test("連續設家甲、乙、丙，再回到較舊的一段：資料夾那一列一直是最後設的家", async ({ page }, info) => {
  const soul = "TreeChain";
  const { email, token } = await newSoulAccount("chain", [soul]);
  const titles = ["連甲", "連乙", "連丙"];
  await seed(token, titles.map((title, index) => ({ id: `chain-${index}`, title, agentId: soul, updatedAt: 3 - index })));
  await loginInBrowser(page, email, PASSWORD);

  for (const [index, title] of titles.entries()) {
    await setChatAsHome(page, title);
    await expect(row(page, `${soul}，家對話：${title}`)).toBeVisible();
    for (const earlier of titles.slice(0, index)) await expect(row(page, earlier)).toHaveAttribute("aria-level", "2");
  }
  await shot(page, info, "third-home");

  // 乙設過家但沒用過就換掉：回到普通對話，可以接著聊（甲設成家之後也沒聊過，同樣不是舊家；ADR-0306）
  await clickChatInList(page, "連乙");
  await expect(row(page, "連乙")).toHaveAttribute("aria-current", "true");
  await expect(row(page, `${soul}，家對話：連丙`)).toBeVisible();
  await sendMessage(page, "換回舊的那段再送一則");
  await expect.poll(async () => (await modelCalls()).some((call) => call.lastText.includes("換回舊的那段再送一則"))).toBe(true);
  await expect(page.getByRole("button", { name: "重新核對保存結果" })).toHaveCount(0);

  await page.reload();
  await expect(row(page, `${soul}，家對話：連丙`)).toBeVisible();
  await expect(row(page, "連乙")).toHaveAttribute("aria-level", "2");
});

test("設為家只有靈魂的對話看得到；設家後舊家回到子對話；再送一則模型收得到", async ({ page }, info) => {
  const soul = "TreeNine";
  const { email, token } = await newSoulAccount("nine", [soul]);
  await seed(token, [
    { id: "nine-home", title: "九的舊家", agentId: soul, updatedAt: 30 },
    { id: "nine-child", title: "九的新家", agentId: soul, updatedAt: 20 },
    { id: "nine-model", title: "九之模型", agentId: FAKE_MODEL_ID, updatedAt: 10 },
  ]);
  await setChatHome(token, "nine-home", soul);
  await loginInBrowser(page, email, PASSWORD);

  await row(page, `九之模型，${FAKE_MODEL_ID}`).getByRole("button", { name: "會話選項：九之模型" }).click();
  await expect(page.getByRole("menuitem", { name: "重命名" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "設為家對話" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);

  await row(page, "九的新家").getByRole("button", { name: "會話選項：九的新家" }).click();
  await page.getByRole("menuitem", { name: "設為家對話" }).click();

  const newHome = `${soul}，家對話：九的新家`;
  await expect(row(page, newHome)).toBeVisible();
  await expect(row(page, "九的舊家")).toHaveAttribute("aria-level", "2");
  await shot(page, info, "home-changed");

  await row(page, newHome).getByText(soul).click();
  await expect(row(page, newHome)).toHaveAttribute("aria-current", "true");
  await sendMessage(page, "設家後再送一則");
  // 存不進去時根本不會送模型
  await expect.poll(async () => (await modelCalls()).some((call) => call.lastText.includes("設家後再送一則"))).toBe(true);
  await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "重新核對保存結果" })).toHaveCount(0);

  await page.reload();
  await expect(row(page, newHome)).toBeVisible();
  await expect(row(page, "九的舊家")).toHaveAttribute("aria-level", "2");
});

test("專案畫面維持依專案分組，點對話回到聊天與靈魂資料夾", async ({ page }, info) => {
  // 一般帳號的專案根目錄要在工作區裡；這裡不看根目錄，用管理員，標題與靈魂都是這個情境專用的
  const soul = "TreeTen";
  await createSoul(adminToken, soul);
  await createProject(adminToken, "ten-project", "十號專案");
  await seed(adminToken, [
    { id: "ten-in", title: "十之專案內", agentId: soul, updatedAt: 20, projectId: "ten-project" },
    { id: "ten-out", title: "十之專案外", agentId: soul, updatedAt: 10 },
    // 一般帳號不能建群聊對話，放在這個管理員的情境驗
    { id: "ten-group", title: "十之群聊", agentId: "ToukaGroup", updatedAt: 5 },
  ]);
  await loginInBrowser(page);

  await openSideMenu(page);
  await page.getByRole("menuitem", { name: "專案管理", exact: true }).click();
  const projectFolder = row(page, /^專案: 十號專案/);
  await expect(projectFolder).toHaveAttribute("aria-expanded", "true");
  await expect(row(page, "十之專案內")).toHaveAttribute("aria-level", "2");
  await expect(row(page, soul)).toHaveCount(0);
  await shot(page, info, "projects");

  // 專案分組和改版前一樣：點名字也能收合
  await projectFolder.getByText("專案: 十號專案").click();
  await expect(projectFolder).toHaveAttribute("aria-expanded", "false");
  await projectFolder.getByText("專案: 十號專案").click();
  await expect(projectFolder).toHaveAttribute("aria-expanded", "true");

  await row(page, "十之專案內").click();
  await composer(page).waitFor();
  await expect(row(page, soul)).toHaveAttribute("aria-expanded", "true");
  await expect(projectFolder).toHaveCount(0);
  await expect(row(page, "十之專案內")).toHaveAttribute("aria-current", "true");
  await expect(page.getByRole("group", { name: "其他對話" }).getByRole("treeitem", { name: "十之群聊，群聊" })).toHaveAttribute("aria-level", "1");
});

test("資料夾依底下最新的對話排序，送一則就排到前面", async ({ page }, info) => {
  const [older, newer] = ["TreeElevenA", "TreeElevenB"];
  const { email, token } = await newSoulAccount("eleven", [older, newer]);
  await seed(token, [
    { id: "eleven-a", title: "十一之舊", agentId: older, updatedAt: 10 },
    { id: "eleven-b", title: "十一之新", agentId: newer, updatedAt: 20 },
  ]);
  await loginInBrowser(page, email, PASSWORD);

  const folders = page.locator('[role="treeitem"][aria-level="1"][aria-expanded]');
  await expect.poll(() => folders.evaluateAll((items) => items.map((item) => item.getAttribute("aria-label")))).toEqual([newer, older]);

  await row(page, "十一之舊").click();
  await expect(row(page, "十一之舊")).toHaveAttribute("aria-current", "true");
  await sendMessage(page, "排到前面");
  await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
  await expect.poll(() => folders.evaluateAll((items) => items.map((item) => item.getAttribute("aria-label")))).toEqual([older, newer]);
  await shot(page, info, "reordered");
});

test("只用鍵盤：Tab 一下跳過清單、方向鍵、左右收合、Shift+F10、改名輸入框", async ({ page }, info) => {
  const soul = "TreeTwelve";
  const { email, token } = await newSoulAccount("twelve", [soul]);
  await seed(token, [
    { id: "twelve-home", title: "十二的家", agentId: soul, updatedAt: 30 },
    { id: "twelve-a", title: "十二之甲", agentId: soul, updatedAt: 20 },
    { id: "twelve-b", title: "十二之乙", agentId: soul, updatedAt: 10 },
  ]);
  await setChatHome(token, "twelve-home", soul);
  await loginInBrowser(page, email, PASSWORD);
  await expect(treeSkeleton(page)).toHaveCount(0);

  const folder = row(page, `${soul}，家對話：十二的家`);
  await folder.getByText(soul).click();
  await expect(folder).toHaveAttribute("aria-current", "true");

  // 清單只算一站：新對話、無痕之後到清單，再一下就到底部選單
  await page.getByRole("button", { name: "無痕對話" }).focus();
  await page.keyboard.press("Tab");
  await expect(folder).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(sideMenuButton(page)).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(folder).toBeFocused();

  await page.keyboard.press("ArrowDown");
  await expect(row(page, "十二之甲")).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(row(page, "十二之乙")).toBeFocused();
  // 滑鼠不在這一列上，「⋯」靠焦點顯示
  await expect(row(page, "十二之乙").getByRole("button", { name: "會話選項：十二之乙" }).locator("..")).toHaveCSS("opacity", "1");
  await shot(page, info, "focus-ring", row(page, "十二之乙"));
  await page.keyboard.press("Home");
  await expect(folder).toBeFocused();
  await page.keyboard.press("End");
  await expect(row(page, "十二之乙")).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(folder).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(folder).toHaveAttribute("aria-expanded", "false");
  await expect(row(page, "十二之甲")).toHaveCount(0);
  await page.keyboard.press("ArrowRight");
  await expect(folder).toHaveAttribute("aria-expanded", "true");
  await expect(folder).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(row(page, "十二之甲")).toBeFocused();

  await page.keyboard.press("Enter");
  await expect(row(page, "十二之甲")).toHaveAttribute("aria-current", "true");

  // 「⋯」關掉後焦點回到這一列
  await page.keyboard.press("Shift+F10");
  await expect(page.getByRole("menuitem", { name: "重命名" })).toBeVisible();
  await shot(page, info, "menu-by-keyboard");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(row(page, "十二之甲")).toBeFocused();

  // 改名：輸入框要留住焦點（選單收合動畫跑完也一樣），方向鍵是移動游標，不是移動清單
  await page.keyboard.press("Shift+F10");
  await page.getByRole("menuitem", { name: "重命名" }).click();
  const input = page.locator('input[value="十二之甲"]');
  await expect(input).toBeFocused();
  await expect(page.getByRole("menu")).toHaveCount(0);
  await page.waitForTimeout(500);
  await expect(input).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(input).toBeFocused();
  await page.keyboard.press("End");
  await page.keyboard.type("改");
  await page.keyboard.press("Enter");
  await expect(row(page, "十二之甲改")).toBeFocused();

  // 關掉「⋯」後馬上去輸入框：選單淡出跑完也不能把焦點拉回這一列（不然接著按的 Enter 會打在側欄上）。
  // 確定的防線是單元測試 SessionItem.closeFocus.test.tsx；這裡補真的 Radix 與淡出（機器很慢時可能漏抓，不會誤紅）
  await page.keyboard.press("Shift+F10");
  await expect(page.getByRole("menuitem", { name: "重命名" })).toBeVisible();
  await page.keyboard.press("Escape");
  await composer(page).focus();
  await page.waitForTimeout(300);
  await expect(composer(page)).toBeFocused();
});
