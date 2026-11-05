import { expect, test } from "@playwright/test";
import {
  activityPolled,
  breathingAnywhere,
  countActivityRequests,
  delayActivityResponse,
  deleteChatInList,
  expectBreathing,
  expectNotBreathing,
  failActivity,
  logOut,
  newSoulAccount,
  PASSWORD,
  pauseActivity,
  pulseTitle,
  recordListRequests,
  row,
  rowNames,
  savedHints,
  seedChats,
  sendHeld,
  setTabVisibility,
  SNAPSHOT_URL,
} from "./lib/activity";
import { adminLogin, awakenSoulOf, retitleChat, savedMessages, savedSnapshot, waitForFinished } from "./lib/api";
import { FAKE_TEXT_REPLY } from "./lib/constants";
import { holdModelCall, modelCalls, releaseModel } from "./lib/control";
import { clickChatInList, loginInBrowser, sendMessage, shot, submitLogin } from "./lib/ui";

let adminToken = "";

test.beforeAll(async () => {
  adminToken = await adminLogin();
});

// 測試失敗時也要放掉扣住的回覆，不然後端一直等，下一個情境的計數會亂
test.afterEach(async () => {
  await releaseModel();
});

test("等模型回覆：名稱呼吸、切去別段也亮、回覆結束馬上停", async ({ page }, info) => {
  const soul = "ActivityBreathe";
  const { email, token } = await newSoulAccount(adminToken, "breathe", [soul]);
  await seedChats(token, [
    { id: "breathe-a", title: "呼吸甲", agentId: soul, updatedAt: 2 },
    { id: "breathe-b", title: "呼吸乙", agentId: soul, updatedAt: 1 },
  ]);
  await loginInBrowser(page, email, PASSWORD);
  await clickChatInList(page, "呼吸甲");

  await sendHeld(page);
  const working = row(page, "呼吸甲，回覆中");
  await expect(working).toBeVisible();
  await expectBreathing(working.getByTestId("session-name"));
  await shot(page, info, "working");

  await clickChatInList(page, "呼吸乙");
  await expect(working).toBeVisible();
  await expectBreathing(working.getByTestId("session-name"));

  // 查詢扣住：要靠本分頁的登記馬上停，不等下一次查詢
  const resume = await pauseActivity(page);
  await releaseModel();
  await expect(row(page, "呼吸甲，有新內容")).toBeVisible();
  await expectNotBreathing(row(page, "呼吸甲，有新內容").getByTestId("session-name"));
  resume();
});

test("回覆結束時開著這段，馬上切走：不留小點", async ({ page }) => {
  const soul = "ActivityNoDot";
  const { email, token } = await newSoulAccount(adminToken, "nodot", [soul]);
  await seedChats(token, [
    { id: "nodot-a", title: "無點甲", agentId: soul, updatedAt: 2 },
    { id: "nodot-b", title: "無點乙", agentId: soul, updatedAt: 1 },
  ]);
  await loginInBrowser(page, email, PASSWORD);
  await clickChatInList(page, "無點甲");
  await sendMessage(page, "一般訊息");
  await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
  await expect(row(page, "無點甲")).toBeVisible();

  await clickChatInList(page, "無點乙");
  await activityPolled(page);
  await activityPolled(page);
  await expect(row(page, "無點甲")).toBeVisible();
  await expect(row(page, "無點甲").getByTestId("session-status-unseen")).toHaveCount(0);
});

test("心跳第 2/5 輪：寫進度、新對話不用重新整理就出現、跑完留小點", async ({ page }, info) => {
  const soul = "ActivityPulse";
  const { email } = await newSoulAccount(adminToken, "pulse", [soul]);
  await loginInBrowser(page, email, PASSWORD);

  await holdModelCall(2);
  await awakenSoulOf(soul, { pulseCount: 5 });
  const title = pulseTitle(soul);
  const working = row(page, `${title}，心跳 2/5`);
  await expect(working).toBeVisible();
  await expect(working.getByTestId("session-status-text")).toHaveText("心跳 2/5");
  await expectBreathing(working.getByTestId("session-name"));
  await shot(page, info, "heartbeat-progress");

  await releaseModel();
  const done = row(page, `${title}，有新內容`);
  await expect(done).toBeVisible({ timeout: 60_000 });
  await expect(done.getByTestId("session-status-unseen")).toBeVisible();
  expect(await breathingAnywhere(done)).toBe(0);
  await shot(page, info, "heartbeat-finished");
});

test("心跳在兩次查詢之間跑完：新對話出現、順序更新、留小點", async ({ page }, info) => {
  const [quiet, active] = ["ActivityGapB", "ActivityGapA"];
  const { email, token } = await newSoulAccount(adminToken, "gap", [active, quiet]);
  await seedChats(token, [
    { id: "gap-b", title: "比較新的", agentId: quiet, updatedAt: 2 },
    { id: "gap-a", title: "比較舊的", agentId: active, updatedAt: 1 },
  ]);
  await loginInBrowser(page, email, PASSWORD);
  await expect(row(page, "比較新的")).toBeVisible();
  await activityPolled(page);
  const before = await rowNames(page);
  expect(before.indexOf(quiet)).toBeLessThan(before.indexOf(active));

  const resume = await pauseActivity(page);
  await awakenSoulOf(active);
  await waitForFinished(token);
  await expect(row(page, new RegExp(pulseTitle(active)))).toHaveCount(0);

  resume();
  await expect(row(page, `${pulseTitle(active)}，有新內容`)).toBeVisible();
  const after = await rowNames(page);
  expect(after.indexOf(active)).toBeLessThan(after.indexOf(quiet));
  await shot(page, info, "after-gap");
});

test("分頁藏起來時不查；心跳跑完再回來，馬上補上", async ({ page }) => {
  const soul = "ActivityHidden";
  const { email, token } = await newSoulAccount(adminToken, "hidden", [soul]);
  await loginInBrowser(page, email, PASSWORD);
  await activityPolled(page);

  await setTabVisibility(page, "hidden");
  const polls = countActivityRequests(page);
  await awakenSoulOf(soul);
  await waitForFinished(token);
  await page.waitForTimeout(9_000);
  expect(polls.count).toBe(0);

  await setTabVisibility(page, "visible");
  await expect(row(page, `${pulseTitle(soul)}，有新內容`)).toBeVisible({ timeout: 5_000 });
});

test("查狀態不重抓整份清單；新出現的對話再送一則，模型收到、存得進去", async ({ page }) => {
  const soul = "ActivityFollowUp";
  const { email, token } = await newSoulAccount(adminToken, "followup", [soul]);
  await loginInBrowser(page, email, PASSWORD);
  await activityPolled(page);

  const lists = recordListRequests(page);
  await awakenSoulOf(soul);
  const { session_id: sessionId } = await waitForFinished(token);
  const title = pulseTitle(soul);
  await expect(row(page, `${title}，有新內容`)).toBeVisible();
  await activityPolled(page);
  expect(lists).toEqual([]);

  await clickChatInList(page, title);
  const followUp = "新對話的下一則";
  await sendMessage(page, followUp);
  await expect.poll(async () => (await modelCalls()).some((call) => call.lastText.includes(followUp))).toBe(true);
  await expect
    .poll(async () => (await savedMessages(token, sessionId)).some((message) => message.role === "user" && message.content === followUp))
    .toBe(true);
  await expect(savedHints(page)).toHaveCount(0);
});

test("單筆下載暫時失敗：下次補上；重新整理不多打下載", async ({ page }) => {
  const soul = "ActivityRetry";
  const { email, token } = await newSoulAccount(adminToken, "retry", [soul]);
  await loginInBrowser(page, email, PASSWORD);
  await activityPolled(page);

  let downloads = 0;
  await page.route(SNAPSHOT_URL, async (route) => {
    downloads += 1;
    // 後端讀取出錯時真正送出的形狀（資料庫暫時忙也是這個代碼）
    if (downloads === 1) await route.fulfill({ status: 500, json: { detail: { code: "SESSION_SAVE_FAILED" } } });
    else await route.continue();
  });
  await awakenSoulOf(soul);
  const { session_id: sessionId } = await waitForFinished(token);
  const done = row(page, `${pulseTitle(soul)}，有新內容`);
  await expect(done).toBeVisible();
  expect(downloads).toBeGreaterThan(1);
  await page.unroute(SNAPSHOT_URL);

  const afterReload: string[] = [];
  page.on("request", (request) => {
    if (SNAPSHOT_URL.test(request.url()) && request.url().includes(encodeURIComponent(sessionId))) afterReload.push(request.url());
  });
  await page.reload();
  await expect(done).toBeVisible();
  await activityPolled(page);
  await activityPolled(page);
  expect(afterReload).toEqual([]);
});

test("單筆下載回應晚到：清單已有較新的內容就不蓋掉", async ({ page }) => {
  const soul = "ActivityLate";
  const { email, token } = await newSoulAccount(adminToken, "late", [soul]);
  await seedChats(token, [{ id: "late-a", title: "晚到的旁邊", agentId: soul, updatedAt: 1 }]);
  await loginInBrowser(page, email, PASSWORD);
  await activityPolled(page);

  let markFetched!: () => void;
  const fetched = new Promise<void>((resolve) => {
    markFetched = resolve;
  });
  let deliver!: () => void;
  const gate = new Promise<void>((resolve) => {
    deliver = resolve;
  });
  await page.route(SNAPSHOT_URL, async (route) => {
    const response = await route.fetch();
    markFetched();
    await gate;
    await route.fulfill({ response });
  });
  await awakenSoulOf(soul);
  const { session_id: sessionId } = await waitForFinished(token);
  await fetched;

  await retitleChat(token, sessionId, "心跳改過名");
  // 換對話時會重抓整份清單（既有行為），帶回較新的標題
  await clickChatInList(page, "晚到的旁邊");
  await expect(row(page, /^心跳改過名/)).toBeVisible();

  deliver();
  await activityPolled(page);
  await expect(row(page, /^心跳改過名/)).toBeVisible();
  await expect(row(page, new RegExp(pulseTitle(soul)))).toHaveCount(0);
});

test("查詢回應晚到：本分頁剛結束的回覆不再亮、不留小點", async ({ page }) => {
  const soul = "ActivityStale";
  const { email, token } = await newSoulAccount(adminToken, "stale-poll", [soul]);
  await seedChats(token, [{ id: "stale-poll-a", title: "舊查詢", agentId: soul, updatedAt: 1 }]);
  await loginInBrowser(page, email, PASSWORD);
  await clickChatInList(page, "舊查詢");
  await sendHeld(page);
  await expect(row(page, "舊查詢，回覆中")).toBeVisible();

  // 這次查詢在回覆結束前就拿到結果，寫著還在回覆
  const late = await delayActivityResponse(page);
  await late.fetched;
  await releaseModel();
  await expect(row(page, "舊查詢")).toBeVisible();

  late.deliver();
  await activityPolled(page);
  await expect(row(page, "舊查詢")).toBeVisible();
  await expect(row(page, "舊查詢").getByTestId("session-status-unseen")).toHaveCount(0);
});

test("換帳號：活動與小點不殘留，舊身分的查詢丟掉；換回來小點還在", async ({ page }, info) => {
  const [mine, theirs] = ["ActivitySwitchA", "ActivitySwitchB"];
  const first = await newSoulAccount(adminToken, "switch-a", [mine]);
  const second = await newSoulAccount(adminToken, "switch-b", [theirs]);
  await loginInBrowser(page, first.email, PASSWORD);
  await activityPolled(page);

  const title = pulseTitle(mine);
  await awakenSoulOf(mine);
  const finished = await waitForFinished(first.token);
  await expect(row(page, `${title}，有新內容`)).toBeVisible();

  await holdModelCall(1);
  await awakenSoulOf(mine);
  await expect(row(page, `${title}，心跳 1/1`)).toBeVisible();

  // 帶著還在路上的舊查詢換帳號
  const resume = await pauseActivity(page);
  await logOut(page);
  await submitLogin(page, second.email, PASSWORD);
  resume();
  await activityPolled(page);
  await activityPolled(page);
  await expect(row(page, new RegExp(mine))).toHaveCount(0);
  await expect(page.getByTestId("session-status-unseen")).toHaveCount(0);
  await expect(page.getByTestId("session-status-text")).toHaveCount(0);
  const stored = await page.evaluate((key) => localStorage.getItem(key), `touka_session_unseen:${second.id}`);
  expect(stored ?? "").not.toContain(finished.session_id);
  await shot(page, info, "other-account");

  await releaseModel();
  // 登出之後這個帳號原本的權杖不能用了
  await waitForFinished(adminToken, finished.session_id, finished.finished_at);
  await logOut(page);
  await submitLogin(page, first.email, PASSWORD);
  await expect(row(page, `${title}，有新內容`)).toBeVisible();
});

test("第 6 筆以後在工作不收起來；資料夾、側欄收起來時也亮", async ({ page }, info) => {
  const soul = "ActivityPinned";
  const { email, token } = await newSoulAccount(adminToken, "pinned", [soul]);
  await seedChats(token, [1, 2, 3, 4, 5, 6, 7].map((n) => ({ id: `pinned-${n}`, title: `七之${n}`, agentId: soul, updatedAt: n })));
  await loginInBrowser(page, email, PASSWORD);
  await expect(row(page, "還有 2 個")).toBeVisible();
  await expect(row(page, "七之1")).toHaveCount(0);
  await activityPolled(page);

  await holdModelCall(1);
  await awakenSoulOf(soul, { sessionId: "pinned-1" });
  const working = row(page, "七之1，心跳 1/1");
  await expect(working).toBeVisible();
  await shot(page, info, "pinned");

  await row(page, soul).getByTestId("session-tree-toggle").click();
  await expect(working).toHaveCount(0);
  const folder = row(page, `${soul}，心跳 1/1`);
  await expect(folder).toBeVisible();
  await expectBreathing(folder.getByTestId("session-name"));
  await shot(page, info, "folder-collapsed");

  await page.getByRole("button", { name: "收合側邊欄" }).click();
  const expand = page.getByRole("button", { name: "展開側邊欄，心跳 1/1" });
  await expect(expand).toBeVisible();
  await expect(expand.getByTestId("session-attention-dot")).toHaveAttribute("data-kind", "heartbeat");
  await expectBreathing(expand.getByTestId("session-attention-dot"));
  await shot(page, info, "sidebar-collapsed");

  await releaseModel();
  await expect(page.getByRole("button", { name: "展開側邊欄，有新內容" })).toBeVisible();
  await expect(page.getByRole("button", { name: "展開側邊欄，有新內容" }).getByTestId("session-attention-dot")).toHaveAttribute("data-kind", "unseen");
});

test("查詢失敗：不轉換、連續 3 次清掉心跳且不留小點、不跳錯誤、恢復後正常", async ({ page }, info) => {
  const soul = "ActivityFail";
  const { email, token } = await newSoulAccount(adminToken, "fail", [soul]);
  await loginInBrowser(page, email, PASSWORD);
  await activityPolled(page);

  await holdModelCall(1);
  await awakenSoulOf(soul);
  const title = pulseTitle(soul);
  const working = row(page, `${title}，心跳 1/1`);
  await expect(working).toBeVisible();

  const recover = await failActivity(page);
  await activityPolled(page);
  await expect(working).toBeVisible();
  await activityPolled(page);
  await expect(working).toBeVisible();
  await activityPolled(page);
  await expect(row(page, title)).toBeVisible();
  await expect(row(page, title).getByTestId("session-status-unseen")).toHaveCount(0);
  await expect(page.locator("[data-sonner-toast]")).toHaveCount(0);
  await shot(page, info, "failed-three-times");

  await recover();
  await expect(working).toBeVisible();

  await releaseModel();
  await waitForFinished(token);
  await expect(row(page, `${title}，有新內容`)).toBeVisible();
});

test("刪掉 Pulse 後下次心跳重建同一個編號：自動出現，再送一則正常", async ({ page }) => {
  const soul = "ActivityRebuilt";
  const { email, token } = await newSoulAccount(adminToken, "rebuilt", [soul]);
  await loginInBrowser(page, email, PASSWORD);
  await activityPolled(page);

  await awakenSoulOf(soul);
  const finished = await waitForFinished(token);
  const title = pulseTitle(soul);
  await expect(row(page, `${title}，有新內容`)).toBeVisible();

  await deleteChatInList(page, title);
  await expect(row(page, new RegExp(title))).toHaveCount(0);

  await awakenSoulOf(soul);
  await waitForFinished(token, finished.session_id, finished.finished_at);
  await expect(row(page, `${title}，有新內容`)).toBeVisible();

  await clickChatInList(page, title);
  const followUp = "重建之後的下一則";
  await sendMessage(page, followUp);
  await expect
    .poll(async () => (await savedMessages(token, finished.session_id)).some((message) => message.role === "user" && message.content === followUp))
    .toBe(true);
  await expect(savedHints(page)).toHaveCount(0);
});

test("鍵盤停在某一列時別的資料夾排到前面：焦點留在原本那一列", async ({ page }) => {
  const [top, bottom] = ["ActivityFocusA", "ActivityFocusB"];
  const { email, token } = await newSoulAccount(adminToken, "focus", [top, bottom]);
  await seedChats(token, [
    { id: "focus-a", title: "焦點上面", agentId: top, updatedAt: 2 },
    { id: "focus-b", title: "焦點下面", agentId: bottom, updatedAt: 1 },
  ]);
  await loginInBrowser(page, email, PASSWORD);
  await expect(row(page, "焦點上面")).toBeVisible();
  await activityPolled(page);

  // 下面的資料夾跳上來時，React 搬動的是上面那幾列
  await row(page, "焦點上面").focus();
  await awakenSoulOf(bottom);
  await waitForFinished(token);
  await expect(row(page, `${pulseTitle(bottom)}，有新內容`)).toBeVisible();
  const names = await rowNames(page);
  expect(names.indexOf(bottom)).toBeLessThan(names.indexOf(top));

  await expect(row(page, "焦點上面")).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await expect(row(page, top)).toBeFocused();
});

test("改名到一半別的資料夾排到前面：輸入框留著、不提前存", async ({ page }) => {
  const [top, bottom] = ["ActivityRenameA", "ActivityRenameB"];
  const { email, token } = await newSoulAccount(adminToken, "rename", [top, bottom]);
  await seedChats(token, [
    { id: "rename-a", title: "改名上面", agentId: top, updatedAt: 2 },
    { id: "rename-b", title: "改名下面", agentId: bottom, updatedAt: 1 },
  ]);
  await loginInBrowser(page, email, PASSWORD);
  await expect(row(page, "改名上面")).toBeVisible();
  await activityPolled(page);

  await page.getByRole("button", { name: "會話選項：改名上面" }).click();
  await page.getByRole("menuitem", { name: "重命名" }).click();
  const input = page.getByRole("textbox", { name: "重命名" });
  await input.fill("改到一半");

  await awakenSoulOf(bottom);
  await waitForFinished(token);
  await expect(row(page, `${pulseTitle(bottom)}，有新內容`)).toBeVisible();
  const names = await rowNames(page);
  expect(names.indexOf(bottom)).toBeLessThan(names.indexOf(top));

  await expect(input).toBeFocused();
  await expect(input).toHaveValue("改到一半");
  expect((await savedSnapshot(token, "rename-a")).session.title).toBe("改名上面");

  await input.fill("改好了");
  await page.keyboard.press("Enter");
  await expect(row(page, "改好了")).toBeVisible();
  await expect.poll(async () => (await savedSnapshot(token, "rename-a")).session.title).toBe("改好了");
});
