import { expect, test, type Page } from "@playwright/test";
import {
  activityPolled,
  breathingAnywhere,
  expectBreathing,
  expectNotBreathing,
  newSoulAccount,
  PASSWORD,
  pulseTitle,
  row,
  savedHints,
  seedChats,
  sendHeld,
} from "./lib/activity";
import { adminLogin, awakenSoulOf, savedMessages, sessionActivity, stopChat, waitForFinished } from "./lib/api";
import {
  ASK_ANSWER,
  ASK_QUESTION,
  askRequest,
  FAKE_AFTER_TOOL_REPLY,
  FAKE_TEXT_REPLY,
  MCP_FORM_MESSAGE,
  mcpRequest,
  MODEL_HOLD_REQUEST,
  writeRequest,
} from "./lib/constants";
import { holdModelCall, modelHolding, releaseModel } from "./lib/control";
import { approveButton } from "./lib/hook_checks";
import { clickChatInList, loginInBrowser, openSideMenu, selectSoul, sendMessage, shot } from "./lib/ui";

let adminToken = "";

test.beforeAll(async () => {
  adminToken = await adminLogin();
});

test.afterEach(async () => {
  await releaseModel();
});

const AWAITING = "等您處理";

function answerAsk(page: Page) {
  return page.getByText(ASK_ANSWER, { exact: true });
}

function submitAsk(page: Page) {
  return page.getByRole("button", { name: "送出回答" });
}

test("核准卡：不動的圖示、沒有動畫；批准或拒絕後消失", async ({ page }, info) => {
  const soul = "AttentionApprove";
  const { email, token } = await newSoulAccount(adminToken, "approve", [soul]);
  await seedChats(token, [{ id: "approve-a", title: "核准甲", agentId: soul, updatedAt: 1 }]);
  await loginInBrowser(page, email, PASSWORD);
  await clickChatInList(page, "核准甲");

  await sendMessage(page, writeRequest("activity-approve.txt"));
  await expect(approveButton(page)).toBeVisible();
  const waiting = row(page, `核准甲，${AWAITING}`);
  await expect(waiting.getByTestId("session-status-awaiting")).toBeVisible();
  expect(await breathingAnywhere(waiting)).toBe(0);
  await shot(page, info, "approval-waiting");

  await approveButton(page).click();
  await expect(waiting).toHaveCount(0);
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();

  await sendMessage(page, writeRequest("activity-deny.txt"));
  await expect(waiting).toBeVisible();
  await page.getByRole("button", { name: "拒絕", exact: true }).click();
  await expect(waiting).toHaveCount(0);
});

test("提問與外掛工具要回應：亮等您處理，答完消失", async ({ page }, info) => {
  const soul = "AttentionAsk";
  const { email, token } = await newSoulAccount(adminToken, "ask", [soul]);
  await seedChats(token, [{ id: "ask-a", title: "提問甲", agentId: soul, updatedAt: 1 }]);
  await loginInBrowser(page, email, PASSWORD);
  await clickChatInList(page, "提問甲");

  await sendMessage(page, askRequest());
  await expect(page.getByText(ASK_QUESTION).first()).toBeVisible();
  const waiting = row(page, `提問甲，${AWAITING}`);
  await expect(waiting.getByTestId("session-status-awaiting")).toBeVisible();
  expect(await breathingAnywhere(waiting)).toBe(0);
  await shot(page, info, "ask-waiting");

  await answerAsk(page).click();
  await submitAsk(page).click();
  await expect(waiting).toHaveCount(0);
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();

  await sendMessage(page, mcpRequest());
  const submit = page.getByRole("button", { name: "送出並繼續" });
  await expect
    .poll(async () => {
      if (await approveButton(page).isVisible()) await approveButton(page).click();
      return submit.isVisible();
    })
    .toBe(true);
  await expect(page.getByText(MCP_FORM_MESSAGE).first()).toBeVisible();
  await expect(waiting).toBeVisible();
  await page.getByRole("textbox", { name: "答案" }).fill("實際驗證的答案");
  await submit.click();
  await expect(waiting).toHaveCount(0);
});

test("兩段對話同時問：兩段都亮；答完跳下一筆、選項不帶上一筆；排隊中那段結束就不再跳出", async ({ page }, info) => {
  const soul = "AttentionQueue";
  const { email, token } = await newSoulAccount(adminToken, "queue", [soul]);
  await seedChats(token, [
    { id: "queue-a", title: "排隊甲", agentId: soul, updatedAt: 3 },
    { id: "queue-b", title: "排隊乙", agentId: soul, updatedAt: 2 },
    { id: "queue-c", title: "排隊丙", agentId: soul, updatedAt: 1 },
  ]);
  await loginInBrowser(page, email, PASSWORD);

  // 彈窗會擋住側欄：先都送出去，再一起放行。假模型每段的工具呼叫編號都一樣（Gemini 也是流水號），問題不能撞在一起
  const ask = `${MODEL_HOLD_REQUEST} ${askRequest()}`;
  const titles = ["排隊甲", "排隊乙", "排隊丙"];
  for (const [index, title] of titles.entries()) {
    await clickChatInList(page, title);
    await sendMessage(page, ask);
    await expect(row(page, `${title}，回覆中`)).toBeVisible();
    await modelHolding(index + 1);
  }
  await releaseModel();

  for (const title of titles) await expect(row(page, `${title}，${AWAITING}`)).toBeVisible();
  await shot(page, info, "all-waiting");

  await expect(page.getByRole("dialog").getByText(ASK_QUESTION)).toBeVisible();
  await answerAsk(page).click();
  await submitAsk(page).click();
  await expect(row(page, new RegExp(AWAITING))).toHaveCount(2);

  // 下一筆是新的表單，沒帶上一筆選的答案
  await expect(page.getByRole("dialog").getByText(ASK_QUESTION)).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("button", { pressed: true })).toHaveCount(0);

  // 另一個裝置按停止：不管停的是畫面上那筆還是排隊中那筆，停掉的都不再跳出，只剩一筆要答
  const remaining = await row(page, new RegExp(AWAITING)).evaluateAll((items) => items.map((item) => item.getAttribute("data-tree-row") ?? ""));
  expect(remaining).toHaveLength(2);
  await stopChat(token, remaining.at(-1) ?? "");
  await expect(row(page, new RegExp(AWAITING))).toHaveCount(1);

  await answerAsk(page).click();
  await submitAsk(page).click();
  await expect(row(page, new RegExp(AWAITING))).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("重新整理：提問不亮（既有限制），核准卡仍亮", async ({ page }) => {
  const soul = "AttentionReload";
  const { email, token } = await newSoulAccount(adminToken, "reload", [soul]);
  await seedChats(token, [
    { id: "reload-a", title: "整理甲", agentId: soul, updatedAt: 2 },
    { id: "reload-b", title: "整理乙", agentId: soul, updatedAt: 1 },
  ]);
  await loginInBrowser(page, email, PASSWORD);

  try {
    await clickChatInList(page, "整理甲");
    await sendMessage(page, writeRequest("activity-reload.txt"));
    await expect(approveButton(page)).toBeVisible();
    await page.reload();
    await expect(row(page, `整理甲，${AWAITING}`)).toBeVisible();

    await clickChatInList(page, "整理乙");
    await sendMessage(page, askRequest());
    await expect(page.getByText(ASK_QUESTION).first()).toBeVisible();
    await expect(row(page, `整理乙，${AWAITING}`)).toBeVisible();
    await page.reload();
    await expect(row(page, "整理乙，回覆中")).toBeVisible();
    await expect(row(page, `整理甲，${AWAITING}`)).toBeVisible();
    await expect(row(page, `整理乙，${AWAITING}`)).toHaveCount(0);
  } finally {
    // 核准卡和提問沒人回，逾時（核准 180 秒）後代理人會再呼叫一次模型，會被後面的測試「扣住第幾次」抓走。
    // 中途失敗也要停；停止本身出錯時不蓋掉原本的失敗，兩段都照停（停不掉的話下面那行會紅）。
    await Promise.allSettled([stopChat(token, "reload-a"), stopChat(token, "reload-b")]);
  }
  await expect.poll(async () => (await sessionActivity(token)).streams).toEqual([]);
});

test("看別段對話時回覆完成：留小點，打開消失，重新整理也一樣", async ({ page }, info) => {
  const soul = "AttentionUnseen";
  const { email, token } = await newSoulAccount(adminToken, "unseen", [soul]);
  await seedChats(token, [
    { id: "unseen-a", title: "小點甲", agentId: soul, updatedAt: 2 },
    { id: "unseen-b", title: "小點乙", agentId: soul, updatedAt: 1 },
  ]);
  await loginInBrowser(page, email, PASSWORD);
  await clickChatInList(page, "小點甲");
  await sendHeld(page);
  await expect(row(page, "小點甲，回覆中")).toBeVisible();

  await clickChatInList(page, "小點乙");
  await releaseModel();
  const unseen = row(page, "小點甲，有新內容");
  await expect(unseen.getByTestId("session-status-unseen")).toBeVisible();
  await shot(page, info, "unseen");

  await page.reload();
  await expect(unseen.getByTestId("session-status-unseen")).toBeVisible();

  await unseen.getByTestId("session-name").click();
  await expect(row(page, "小點甲")).toHaveAttribute("aria-current", "true");
  await page.reload();
  await expect(row(page, "小點甲")).toBeVisible();
  await expect(page.getByTestId("session-status-unseen")).toHaveCount(0);
});

test("在設定畫面時目前對話算沒在看：留小點，回到聊天清掉", async ({ page }, info) => {
  const soul = "AttentionSettings";
  const { email, token } = await newSoulAccount(adminToken, "settings", [soul]);
  await seedChats(token, [{ id: "settings-a", title: "設定甲", agentId: soul, updatedAt: 1 }]);
  await loginInBrowser(page, email, PASSWORD);
  await clickChatInList(page, "設定甲");
  await sendHeld(page);
  await expect(row(page, "設定甲，回覆中")).toBeVisible();

  await openSideMenu(page);
  await page.getByRole("menuitem", { name: "設定", exact: true }).click();
  await releaseModel();
  const unseen = row(page, "設定甲，有新內容");
  await expect(unseen.getByTestId("session-status-unseen")).toBeVisible();
  await shot(page, info, "settings-unseen");

  await unseen.getByTestId("session-name").click();
  await expect(row(page, "設定甲")).toBeVisible();
  await expect(page.getByTestId("session-status-unseen")).toHaveCount(0);
});

test("減少動態：App 開、系統開時不動但有文字；App 關但系統開照 App 設定", async ({ page }, info) => {
  const soul = "AttentionMotion";
  const { email, token } = await newSoulAccount(adminToken, "motion", [soul]);
  await seedChats(token, [{ id: "motion-a", title: "動態甲", agentId: soul, updatedAt: 1 }]);
  await loginInBrowser(page, email, PASSWORD);
  await clickChatInList(page, "動態甲");
  await sendHeld(page);
  const working = row(page, "動態甲，回覆中");
  await expect(working).toBeVisible();

  const setMotion = async (setting: string, system: "reduce" | "no-preference") => {
    await page.emulateMedia({ reducedMotion: system });
    await page.evaluate((value) => localStorage.setItem("touka_reduce_motion", value), setting);
    await page.reload();
    await expect(working).toBeVisible();
  };

  await setMotion("on", "no-preference");
  await expect(working.getByTestId("session-status-text")).toHaveText("回覆中");
  await expectNotBreathing(working.getByTestId("session-name"));
  await shot(page, info, "app-on");

  await setMotion("system", "reduce");
  await expect(working.getByTestId("session-status-text")).toHaveText("回覆中");
  await expectNotBreathing(working.getByTestId("session-name"));

  await setMotion("off", "reduce");
  await expect(working.getByTestId("session-status-text")).toHaveCount(0);
  await expectBreathing(working.getByTestId("session-name"));
});

test("無痕：查詢不回、瀏覽器不存編號", async ({ page }) => {
  const soul = "AttentionIncognito";
  const { email, token } = await newSoulAccount(adminToken, "incognito", [soul]);
  await seedChats(token, [{ id: "incognito-a", title: "一般的那段", agentId: soul, updatedAt: 1 }]);
  await loginInBrowser(page, email, PASSWORD);

  await page.getByRole("button", { name: "無痕對話", exact: true }).click();
  await selectSoul(page, soul);
  await sendHeld(page);
  const working = row(page, /回覆中，無痕/);
  await expect(working).toBeVisible();
  const sessionId = (await working.getAttribute("data-tree-row")) ?? "";
  expect(sessionId).not.toBe("");
  expect((await sessionActivity(token)).streams).toEqual([]);

  // 看別段時完成：小點只放記憶體
  await clickChatInList(page, "一般的那段");
  await releaseModel();
  await expect(row(page, /有新內容，無痕/).getByTestId("session-status-unseen")).toBeVisible();
  const stored = () => page.evaluate(() => JSON.stringify(Object.entries(localStorage)));
  expect(await stored()).not.toContain(sessionId);

  // 回覆中重新整理：一樣不留
  await page.getByRole("button", { name: "無痕對話", exact: true }).click();
  await selectSoul(page, soul);
  await sendHeld(page);
  const second = row(page, /回覆中，無痕/);
  await expect(second).toBeVisible();
  const secondId = (await second.getAttribute("data-tree-row")) ?? "";
  expect(await stored()).not.toContain(secondId);
  await page.reload();
  await expect(row(page, "一般的那段")).toBeVisible();
  expect(await stored()).not.toContain(secondId);
  expect((await sessionActivity(token)).streams).toEqual([]);
});

test("可見範圍：自己靈魂的心跳看得到、別人的看不到、管理員看得到；別人的串流誰都看不到", async ({ page }) => {
  const soul = "AttentionVisible";
  const owner = await newSoulAccount(adminToken, "visible-owner", [soul]);
  const stranger = await newSoulAccount(adminToken, "visible-stranger", []);
  await seedChats(owner.token, [{ id: "visible-a", title: "可見甲", agentId: soul, updatedAt: 1 }]);

  await holdModelCall(1);
  await awakenSoulOf(soul);
  await expect.poll(async () => (await sessionActivity(owner.token)).background.length).toBe(1);
  const sessionId = (await sessionActivity(owner.token)).background[0].session_id;
  expect((await sessionActivity(adminToken)).background.map((item) => item.session_id)).toContain(sessionId);
  const since = (await sessionActivity(stranger.token)).cursor;
  expect((await sessionActivity(stranger.token, since)).background).toEqual([]);
  await releaseModel();
  await waitForFinished(owner.token, sessionId);
  // 別人的心跳結束：回應逐字不變
  expect(await sessionActivity(stranger.token, since)).toEqual({ streams: [], background: [], finished: [], cursor: since });

  await loginInBrowser(page, owner.email, PASSWORD);
  await clickChatInList(page, "可見甲");
  await sendHeld(page);
  await expect(row(page, "可見甲，回覆中")).toBeVisible();
  await expect.poll(async () => (await sessionActivity(owner.token)).streams).toEqual(["visible-a"]);
  expect((await sessionActivity(adminToken)).streams).not.toContain("visible-a");
  expect((await sessionActivity(stranger.token)).streams).toEqual([]);
});

test.describe("手機", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true });

  test("抽屜關著：選單鈕亮；打開後名稱前的小點呼吸；完成後留小點", async ({ page }, info) => {
    const soul = "AttentionMobile";
    const { email, token } = await newSoulAccount(adminToken, "mobile", [soul]);
    await loginInBrowser(page, email, PASSWORD);
    await activityPolled(page);
    // 手機第一次進來抽屜是開的
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);

    await holdModelCall(1);
    await awakenSoulOf(soul);
    const title = pulseTitle(soul);
    const menuButton = page.getByRole("button", { name: /，心跳 1\/1$/ });
    await expect(menuButton).toBeVisible();
    await expectBreathing(menuButton.getByTestId("session-attention-dot"));
    await shot(page, info, "menu-button-working");

    await menuButton.tap();
    const working = row(page, `${title}，心跳 1/1`);
    await expect(working).toBeVisible();
    await expectBreathing(working.getByTestId("session-status-working-dot"));
    await expectNotBreathing(working.getByTestId("session-name"));
    await shot(page, info, "drawer-working");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);

    await releaseModel();
    await waitForFinished(token);
    const done = page.getByRole("button", { name: /，有新內容$/ });
    await expect(done).toBeVisible();
    await expect(done.getByTestId("session-attention-dot")).toHaveAttribute("data-kind", "unseen");
    await done.tap();
    await expect(row(page, `${title}，有新內容`).getByTestId("session-status-unseen")).toBeVisible();
    await shot(page, info, "drawer-finished");
  });
});

test("開著的對話被心跳寫入：出現提示，點了內容更新，再送一則正常", async ({ page }, info) => {
  const soul = "AttentionStale";
  const { email, token } = await newSoulAccount(adminToken, "stale-notice", [soul]);
  await seedChats(token, [{ id: "stale-a", title: "提示甲", agentId: soul, updatedAt: 1 }]);
  await loginInBrowser(page, email, PASSWORD);
  await clickChatInList(page, "提示甲");
  await activityPolled(page);

  await awakenSoulOf(soul, { sessionId: "stale-a" });
  await waitForFinished(token, "stale-a");
  const refresh = page.getByRole("button", { name: "點一下更新" });
  await expect(refresh).toBeVisible();
  await expect(page.getByText(FAKE_TEXT_REPLY)).toHaveCount(0);
  await shot(page, info, "notice");

  await refresh.click();
  await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
  await expect(refresh).toHaveCount(0);

  const followUp = "更新之後的下一則";
  await sendMessage(page, followUp);
  await expect
    .poll(async () => (await savedMessages(token, "stale-a")).some((message) => message.role === "user" && message.content === followUp))
    .toBe(true);
  await expect(savedHints(page)).toHaveCount(0);
});

test("提示還在時就送一則：存檔失敗的畫面取代提示", async ({ page }, info) => {
  const soul = "AttentionFailed";
  const { email, token } = await newSoulAccount(adminToken, "failed-notice", [soul]);
  await seedChats(token, [{ id: "failed-a", title: "失敗甲", agentId: soul, updatedAt: 1 }]);
  await loginInBrowser(page, email, PASSWORD);
  await clickChatInList(page, "失敗甲");
  await activityPolled(page);

  await awakenSoulOf(soul, { sessionId: "failed-a", initPrompt: "實際驗證：心跳寫入" });
  await waitForFinished(token, "failed-a");
  const refresh = page.getByRole("button", { name: "點一下更新" });
  await expect(refresh).toBeVisible();

  await sendMessage(page, "沒更新就送");
  await expect(page.getByRole("button", { name: "重新核對保存結果" })).toBeVisible();
  await expect(refresh).toHaveCount(0);
  await shot(page, info, "save-failed");
});
