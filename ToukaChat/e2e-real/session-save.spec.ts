import { expect, test, type Page } from "@playwright/test";
import { adminLogin, disableHooks, latestSessionId, savedMessages, savedSnapshot } from "./lib/api";
import { FAKE_AFTER_TOOL_REPLY, FAKE_TEXT_REPLY, MODEL_400_REQUEST, SOUL_ID, writeRequest } from "./lib/constants";
import { modelCalls, resetRecorders } from "./lib/control";
import { approveButton } from "./lib/hook_checks";
import {
  composer,
  expectChatIsHome,
  expectLastReplyMarkedFailed,
  loginInBrowser,
  openChat,
  recoverSave,
  renameChat,
  selectSoul,
  sendMessage,
  setChatAsHome,
  shot,
  unsetChatHome,
} from "./lib/ui";

let adminToken = "";

async function expectNoSaveNotice(page: Page): Promise<void> {
  await expect(page.getByRole("button", { name: "重新核對保存結果" })).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: /保存/ })).toHaveCount(0);
}

/** 畫面上這一輪結束了（收尾保存也做完才會收起停止鈕）。 */
async function turnEnded(page: Page): Promise<void> {
  await expect(page.getByTitle("停止")).toHaveCount(0);
}

/**
 * 送出一則，等模型收到、這一輪收尾，而且沒有保存提示。
 * 存不進去時根本不會送模型，紅在等模型那一步。
 */
async function send(page: Page, text: string): Promise<void> {
  await sendMessage(page, text);
  await expect
    .poll(async () => (await modelCalls()).some((call) => call.lastText.includes(text)), `模型收到「${text}」`)
    .toBe(true);
  await turnEnded(page);
  await expectNoSaveNotice(page);
}

async function savedUserTexts(sessionId: string): Promise<string[]> {
  const messages = await savedMessages(adminToken, sessionId);
  return messages.filter((message) => message.role === "user").map((message) => message.content ?? "");
}

/** 重新整理，等畫面查完這段對話的串流狀態、該做的請求都做完。 */
async function reloadAndSettle(page: Page): Promise<void> {
  const checked = page.waitForResponse((response) => response.url().includes("/stream/state"));
  await page.reload();
  await composer(page).waitFor();
  await checked;
  await page.waitForLoadState("networkidle");
}

// API 準備要排在瀏覽器登入之前：再登入會換掉瀏覽器的 refresh token。
test.beforeEach(async ({ page }) => {
  adminToken = await adminLogin();
  await disableHooks(adminToken);
  await resetRecorders();
  await loginInBrowser(page);
  await openChat(page);
  await selectSoul(page);
});

test("這一頁新建的對話設為家之後，下一則直接送出", async ({ page }, info) => {
  await send(page, "設家之前");
  const id = await latestSessionId(adminToken);
  await setChatAsHome(page);
  await expect.poll(async () => (await savedSnapshot(adminToken, id)).session.is_home).toBe(true);

  await send(page, "設家之後");

  expect(await savedUserTexts(id)).toEqual(expect.arrayContaining(["設家之前", "設家之後"]));
  const { session } = await savedSnapshot(adminToken, id);
  expect(session).toMatchObject({ is_home: true, home_for_agent: SOUL_ID });
  await shot(page, info, "after-next-message");
});

test("這一頁新建的對話，點一下開著的這段再送：下一則直接送出", async ({ page }) => {
  await send(page, "點之前");
  const id = await latestSessionId(adminToken);

  // 點對話會重新載入清單，清單沒有版本，下一則要重讀存檔
  await page.getByText("點之前", { exact: true }).first().click();
  await page.waitForLoadState("networkidle");

  await send(page, "點之後");
  expect(await savedUserTexts(id)).toEqual(expect.arrayContaining(["點之前", "點之後"]));
});

test("新對話 A、新對話 B 各送一則，點回 A 再送：下一則直接送出", async ({ page }) => {
  await send(page, "甲段");
  const first = await latestSessionId(adminToken);

  await page.getByRole("button", { name: "新增對話" }).click();
  await selectSoul(page);
  await send(page, "乙段");
  const second = await latestSessionId(adminToken);
  expect(second).not.toBe(first);

  await page.getByText("甲段", { exact: true }).first().click();
  await page.waitForLoadState("networkidle");

  await send(page, "甲段：切回來之後");
  expect(await savedUserTexts(first)).toEqual(expect.arrayContaining(["甲段", "甲段：切回來之後"]));
  expect(await savedUserTexts(second)).not.toContain("甲段：切回來之後");
});

test("設為家又馬上取消，再送一則都正常", async ({ page }) => {
  await send(page, "取消之前");
  const id = await latestSessionId(adminToken);
  await setChatAsHome(page);
  await expect.poll(async () => (await savedSnapshot(adminToken, id)).session.is_home).toBe(true);

  await unsetChatHome(page, "取消之前");
  await expect.poll(async () => (await savedSnapshot(adminToken, id)).session.is_home).toBe(false);
  await expectChatIsHome(page, "取消之前", false);

  await send(page, "取消之後");
  expect(await savedUserTexts(id)).toContain("取消之後");
  expect((await savedSnapshot(adminToken, id)).session).toMatchObject({ is_home: false, home_for_agent: null });
});

test("設為家之後改名，再送一則都正常", async ({ page }) => {
  await send(page, "改名之前");
  const id = await latestSessionId(adminToken);
  await setChatAsHome(page);
  await expect.poll(async () => (await savedSnapshot(adminToken, id)).session.is_home).toBe(true);

  await renameChat(page, "改名之前", "新的名字");

  await expect.poll(async () => (await savedSnapshot(adminToken, id)).session.title).toBe("新的名字");
  await expectNoSaveNotice(page);
  await send(page, "改名之後");
  expect(await savedUserTexts(id)).toContain("改名之後");
});

test("輪流換家之後，舊的家對話再送一則都正常", async ({ page }, info) => {
  await send(page, "第一段");
  const first = await latestSessionId(adminToken);
  await setChatAsHome(page, "第一段");
  await expect.poll(async () => (await savedSnapshot(adminToken, first)).session.is_home).toBe(true);

  await page.getByRole("button", { name: "新增對話" }).click();
  await selectSoul(page);
  await send(page, "第二段");
  const second = await latestSessionId(adminToken);
  expect(second).not.toBe(first);
  await setChatAsHome(page, "第二段");
  await expect.poll(async () => (await savedSnapshot(adminToken, second)).session.is_home).toBe(true);
  expect((await savedSnapshot(adminToken, first)).session).toMatchObject({ is_home: false, home_for_agent: null });

  // 第一段現在是舊家
  await page.getByText("第一段", { exact: true }).first().click();
  await send(page, "第一段：換家之後");
  expect(await savedUserTexts(first)).toContain("第一段：換家之後");

  // 換回來，第二段變成舊家
  await setChatAsHome(page, "第一段");
  await expect.poll(async () => (await savedSnapshot(adminToken, first)).session.is_home).toBe(true);
  expect((await savedSnapshot(adminToken, second)).session).toMatchObject({ is_home: false, home_for_agent: null });
  await page.getByText("第二段", { exact: true }).first().click();
  await send(page, "第二段：換家之後");
  expect(await savedUserTexts(second)).toContain("第二段：換家之後");
  await shot(page, info, "old-home-saved");
});

test("連續換兩次家：清單上只剩最後一段是家，舊家再送一則都正常", async ({ page }) => {
  await send(page, "甲家");
  const first = await latestSessionId(adminToken);
  await setChatAsHome(page, "甲家");
  await expect.poll(async () => (await savedSnapshot(adminToken, first)).session.is_home).toBe(true);

  const setNewChatAsHome = async (title: string): Promise<string> => {
    await page.getByRole("button", { name: "新增對話" }).click();
    await selectSoul(page);
    await send(page, title);
    const id = await latestSessionId(adminToken);
    await setChatAsHome(page, title);
    await expect.poll(async () => (await savedSnapshot(adminToken, id)).session.is_home).toBe(true);
    return id;
  };
  const second = await setNewChatAsHome("乙家");
  const third = await setNewChatAsHome("丙家");

  expect((await savedSnapshot(adminToken, second)).session).toMatchObject({ is_home: false, home_for_agent: null });
  await expectChatIsHome(page, "甲家", false);
  await expectChatIsHome(page, "乙家", false);
  await expectChatIsHome(page, "丙家", true);

  await page.getByText("乙家", { exact: true }).first().click();
  await send(page, "乙家：換家之後");
  expect(await savedUserTexts(second)).toContain("乙家：換家之後");
  expect((await savedSnapshot(adminToken, third)).session).toMatchObject({ is_home: true, home_for_agent: SOUL_ID });
});

test("回覆之後重新整理：存檔不變、沒有提示，下一則直接送出", async ({ page }, info) => {
  await send(page, "重新整理之前");
  const id = await latestSessionId(adminToken);
  const before = await savedSnapshot(adminToken, id);

  await reloadAndSettle(page);

  await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
  await expectNoSaveNotice(page);
  expect((await savedSnapshot(adminToken, id)).revision).toBe(before.revision);
  await shot(page, info, "after-reload");

  await send(page, "重新整理之後");
  expect(await savedUserTexts(id)).toEqual(expect.arrayContaining(["重新整理之前", "重新整理之後"]));
});

// 已知限制（ADR-0231）：後端收尾時版本已被 set_home 換掉，會跳提示；按恢復之後內容不會掉。
test("回覆還在跑時設為家：跳提示，按恢復之後內容完整，下一則直接送出", async ({ page }) => {
  await send(page, "還在跑之前");
  const id = await latestSessionId(adminToken);
  // 工具等核准時這一輪還沒結束
  await sendMessage(page, writeRequest(`session-save-${Date.now()}.txt`));
  await approveButton(page).waitFor();
  await setChatAsHome(page, "還在跑之前");
  await expect.poll(async () => (await savedSnapshot(adminToken, id)).session.is_home).toBe(true);

  await approveButton(page).click();
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  await recoverSave(page);
  await turnEnded(page);
  await expectNoSaveNotice(page);
  const replies = (await savedMessages(adminToken, id)).filter((message) => message.role === "assistant");
  expect(replies.at(-1)?.steps?.length ?? 0).toBeGreaterThan(0);

  await send(page, "還在跑之後");
  expect(await savedUserTexts(id)).toContain("還在跑之後");
  expect((await savedSnapshot(adminToken, id)).session).toMatchObject({ is_home: true, home_for_agent: SOUL_ID });
});

test("有工具步驟的回覆，重新整理之後內容仍然完整", async ({ page }, info) => {
  const fileName = `session-save-${Date.now()}.txt`;
  await sendMessage(page, writeRequest(fileName));
  await approveButton(page).click();
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  await turnEnded(page);
  await expectNoSaveNotice(page);
  const id = await latestSessionId(adminToken);
  const before = await savedSnapshot(adminToken, id);

  await reloadAndSettle(page);

  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  await expect(page.locator("[data-index]").last().getByText("write").first()).toBeVisible();
  await expectNoSaveNotice(page);
  expect((await savedSnapshot(adminToken, id)).revision).toBe(before.revision);
  const replies = (await savedMessages(adminToken, id)).filter((message) => message.role === "assistant");
  expect(replies.at(-1)?.steps?.length ?? 0).toBeGreaterThan(0);
  await shot(page, info, "tool-steps-after-reload");
});

test("原頁來不及存完整內容就關掉：重新整理時補回工具步驟", async ({ page }) => {
  // 收尾的完整保存送不出去，存檔只有後端存的文字版
  await page.route("**/v1/ui/sessions/*", async (route) => {
    const body = route.request().postData() ?? "";
    if (route.request().method() === "POST" && body.includes(FAKE_AFTER_TOOL_REPLY)) await route.abort();
    else await route.continue();
  });
  const fileName = `session-save-${Date.now()}.txt`;
  await sendMessage(page, writeRequest(fileName));
  await approveButton(page).click();
  await expect(page.getByRole("button", { name: "重新核對保存結果" })).toBeVisible();
  const id = await latestSessionId(adminToken);
  const textOnly = await savedSnapshot(adminToken, id);
  const stored = (await savedMessages(adminToken, id)).filter((message) => message.role === "assistant");
  expect(stored.at(-1)?.steps ?? []).toHaveLength(0);
  await page.unroute("**/v1/ui/sessions/*");

  await reloadAndSettle(page);

  await turnEnded(page);
  await expectNoSaveNotice(page);
  await expect.poll(async () => (await savedSnapshot(adminToken, id)).revision).not.toBe(textOnly.revision);
  const replies = (await savedMessages(adminToken, id)).filter((message) => message.role === "assistant");
  expect(replies.at(-1)?.steps?.length ?? 0).toBeGreaterThan(0);
});

test("模型失敗、按過恢復之後重新整理：不用再按一次，下一則直接送出", async ({ page }, info) => {
  await sendMessage(page, MODEL_400_REQUEST);
  await expectLastReplyMarkedFailed(page);
  await recoverSave(page);
  const id = await latestSessionId(adminToken);
  const before = await savedSnapshot(adminToken, id);

  await reloadAndSettle(page);

  await expectLastReplyMarkedFailed(page);
  await expectNoSaveNotice(page);
  expect((await savedSnapshot(adminToken, id)).revision).toBe(before.revision);
  await shot(page, info, "failed-reply-after-reload");

  await send(page, "失敗之後的下一則");
  expect(await savedUserTexts(id)).toContain("失敗之後的下一則");
});
