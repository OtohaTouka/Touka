import { expect, test, type Page } from "@playwright/test";
import { adminLogin, disableHooks, latestSessionId, retitleChat, savedMessages, savedSnapshot } from "./lib/api";
import { FAKE_AFTER_TOOL_REPLY, FAKE_TEXT_REPLY, MODEL_400_REQUEST, MODEL_HOLD_REQUEST, SOUL_ID, writeRequest } from "./lib/constants";
import { modelCalls, modelHolding, releaseModel, resetRecorders } from "./lib/control";
import { approveButton } from "./lib/hook_checks";
import {
  clickChatInList,
  composer,
  expectChatIsHome,
  expectLastReplyMarkedFailed,
  expectNoSaveNotice,
  loginInBrowser,
  openChat,
  renameChat,
  selectSoul,
  sendMessage,
  setChatAsHome,
  shot,
  unsetChatHome,
} from "./lib/ui";

let adminToken = "";

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

test.afterEach(async () => {
  await releaseModel();
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
  await clickChatInList(page, "點之前");
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

  await clickChatInList(page, "甲段");
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
  await clickChatInList(page, "第一段");
  await send(page, "第一段：換家之後");
  expect(await savedUserTexts(first)).toContain("第一段：換家之後");

  // 換回來，第二段變成舊家
  await setChatAsHome(page, "第一段");
  await expect.poll(async () => (await savedSnapshot(adminToken, first)).session.is_home).toBe(true);
  expect((await savedSnapshot(adminToken, second)).session).toMatchObject({ is_home: false, home_for_agent: null });
  await clickChatInList(page, "第二段");
  await send(page, "第二段：換家之後");
  expect(await savedUserTexts(second)).toContain("第二段：換家之後");
  await shot(page, info, "old-home-saved");
});

// 資料夾那一列取最新的家標記，要換回比較舊的那段，舊標記沒清掉才看得出來
test("換回比較舊的那段當家：資料夾那一列跟著換", async ({ page }) => {
  await send(page, "舊的一段");
  const older = await latestSessionId(adminToken);

  await page.getByRole("button", { name: "新增對話" }).click();
  await selectSoul(page);
  await send(page, "新的一段");
  const newer = await latestSessionId(adminToken);
  await setChatAsHome(page, "新的一段");
  await expect.poll(async () => (await savedSnapshot(adminToken, newer)).session.is_home).toBe(true);

  await setChatAsHome(page, "舊的一段");
  await expect.poll(async () => (await savedSnapshot(adminToken, older)).session.is_home).toBe(true);

  await expectChatIsHome(page, "舊的一段", true);
  await expectChatIsHome(page, "新的一段", false);
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

  await clickChatInList(page, "乙家");
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

/** 這一輪收尾存好了，存檔裡最後一則回覆有工具步驟。 */
async function expectToolReplySaved(page: Page, id: string): Promise<void> {
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  await turnEnded(page);
  await expectNoSaveNotice(page);
  const replies = (await savedMessages(adminToken, id)).filter((message) => message.role === "assistant");
  expect(replies.at(-1)?.steps?.length ?? 0).toBeGreaterThan(0);
}

// 家標記不算進版本（ADR-0301）：設家不讓回覆收尾的保存存不進去
test("回覆還在跑時設為家：不跳提示，回覆存好，下一則直接送出", async ({ page }) => {
  await send(page, "還在跑之前");
  const id = await latestSessionId(adminToken);
  // 工具等核准時這一輪還沒結束
  await sendMessage(page, writeRequest(`session-save-${Date.now()}.txt`));
  await approveButton(page).waitFor();
  await setChatAsHome(page, "還在跑之前");
  await expect.poll(async () => (await savedSnapshot(adminToken, id)).session.is_home).toBe(true);

  await approveButton(page).click();
  await expectToolReplySaved(page, id);
  await expectChatIsHome(page, "還在跑之前", true);

  await send(page, "還在跑之後");
  expect(await savedUserTexts(id)).toContain("還在跑之後");
  expect((await savedSnapshot(adminToken, id)).session).toMatchObject({ is_home: true, home_for_agent: SOUL_ID });
});

test("回覆還在跑時把家換到另一段：舊家這段不跳提示，回覆存好", async ({ page }) => {
  await send(page, "之後的家");
  const newHome = await latestSessionId(adminToken);
  await page.getByRole("button", { name: "新增對話" }).click();
  await selectSoul(page);
  await send(page, "原本的家");
  const oldHome = await latestSessionId(adminToken);
  expect(oldHome).not.toBe(newHome);
  await setChatAsHome(page, "原本的家");
  await expect.poll(async () => (await savedSnapshot(adminToken, oldHome)).session.is_home).toBe(true);

  await sendMessage(page, writeRequest(`session-save-${Date.now()}.txt`));
  await approveButton(page).waitFor();
  await setChatAsHome(page, "之後的家");
  await expect.poll(async () => (await savedSnapshot(adminToken, newHome)).session.is_home).toBe(true);

  await approveButton(page).click();
  await expectToolReplySaved(page, oldHome);
  await expectChatIsHome(page, "之後的家", true);
  await expectChatIsHome(page, "原本的家", false);

  await send(page, "舊家換掉之後");
  expect(await savedUserTexts(oldHome)).toContain("舊家換掉之後");
  expect((await savedSnapshot(adminToken, oldHome)).session).toMatchObject({ is_home: false, home_for_agent: null });
});

test("回覆收尾的保存延後、這期間設為家：放行後不跳提示，資料夾那一列是新家", async ({ page }) => {
  await send(page, "延後之前");
  const id = await latestSessionId(adminToken);
  // 攔住這一輪收尾的完整保存，設好家才放行
  let held!: () => void;
  const holding = new Promise<void>((resolve) => { held = resolve; });
  let release!: () => void;
  const released = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/v1/ui/sessions/*", async (route) => {
    const body = route.request().postData() ?? "";
    if (route.request().method() === "POST" && body.includes(FAKE_AFTER_TOOL_REPLY)) {
      held();
      await released;
    }
    await route.continue();
  });
  await sendMessage(page, writeRequest(`session-save-${Date.now()}.txt`));
  await approveButton(page).click();
  await holding;
  await setChatAsHome(page, "延後之前");
  await expect.poll(async () => (await savedSnapshot(adminToken, id)).session.is_home).toBe(true);
  release();

  await expectToolReplySaved(page, id);
  await page.unroute("**/v1/ui/sessions/*");
  await expectChatIsHome(page, "延後之前", true);
  await send(page, "延後之後");
  expect(await savedUserTexts(id)).toContain("延後之後");
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

test("模型失敗之後重新整理：不用按恢復，失敗標示還在，下一則直接送出", async ({ page }, info) => {
  await sendMessage(page, MODEL_400_REQUEST);
  await expectLastReplyMarkedFailed(page);
  await turnEnded(page);
  await expectNoSaveNotice(page);
  const id = await latestSessionId(adminToken);
  const before = await savedSnapshot(adminToken, id);
  expect((await savedMessages(adminToken, id)).some((message) => message.generationFailed)).toBe(true);

  await reloadAndSettle(page);

  await expectLastReplyMarkedFailed(page);
  await expectNoSaveNotice(page);
  expect((await savedSnapshot(adminToken, id)).revision).toBe(before.revision);
  await shot(page, info, "failed-reply-after-reload");

  await send(page, "失敗之後的下一則");
  expect(await savedUserTexts(id)).toContain("失敗之後的下一則");
});

test("模型失敗前另一個裝置先改過這段對話：照舊提示，失敗那則不硬存", async ({ page }, info) => {
  // 只扣這一則（看內容，不數第幾次）：別條留下的代理人多呼叫一次模型時，才不會扣錯
  await sendMessage(page, `${MODEL_HOLD_REQUEST} ${MODEL_400_REQUEST}`);
  await modelHolding();
  const id = await latestSessionId(adminToken);
  await retitleChat(adminToken, id, "另一個裝置改過名");
  await releaseModel();

  await expectLastReplyMarkedFailed(page);
  await expect(page.getByRole("button", { name: "重新核對保存結果" })).toBeVisible();
  await shot(page, info, "failed-after-other-device");
  expect((await savedMessages(adminToken, id)).some((message) => message.generationFailed)).toBe(false);
  expect((await savedSnapshot(adminToken, id)).session.title).toBe("另一個裝置改過名");
});
