import { expect, test, type Page, type TestInfo } from "@playwright/test";
import {
  adminLogin,
  awakenSoul,
  disableHooks,
  hookRuns,
  hooksEnabled,
  latestSessionId,
  savedMessages,
  setSlidingWindow,
} from "./lib/api";
import {
  CANARY_API_KEY,
  CANARY_HOOK_HEADER,
  FAKE_MODEL_ID,
  FAKE_TEXT_REPLY,
  MCP_FORM_MESSAGE,
  MCP_SERVER,
  MODEL_400_REQUEST,
  MODEL_BAD_JSON_REQUEST,
  MODEL_EMPTY_REPLY_REQUEST,
  MODEL_NO_REPLY_REQUEST,
  SOUL_ID,
  SUBAGENT_GOAL,
  TASK_SUBJECTS,
  mcpRequest,
  missionRequest,
  subagentRequest,
  taskRequest,
} from "./lib/constants";
import { modelCalls, receivedFor, resetRecorders } from "./lib/control";
import {
  approveButton,
  expectBackgroundHook,
  expectCleanPayload,
  lastToolResultForModel,
} from "./lib/hook_checks";
import { HANG_MS, HOOK_FORM_ANSWER, SUBAGENT_CONTEXT, hookUrl } from "./lib/hook_sink";
import {
  addHttpHook,
  composer,
  enableHooks,
  eventSection,
  expectLastReplyMarkedFailed,
  expectNoSaveNotice,
  loginInBrowser,
  openChat,
  openHooksTab,
  saveHooks,
  selectSoul,
  sendMessage,
  setChatAsHome,
  shot,
} from "./lib/ui";

const PERSON_FORM_ANSWER = "人填的答案";
// 未知模型的 context window 是 128000，trigger_pct 2 就是 2560 token；每則訊息要明顯超過。
const LONG_MESSAGE = "實際驗證剪裁用的長訊息。".repeat(1200);
const MAX_TRIM_ATTEMPTS = 4;
// 後端剪裁等畫面收尾保存的上限（`chat_trim_wait.CLOSING_SAVE_WAIT_SECONDS`，ADR-0302）
const CLOSING_SAVE_WAIT_MS = 10_000;
const SESSION_SAVE_URL = /\/v1\/ui\/sessions\/[^/?]+$/;
const SLIDING_WINDOW_OFF = { enabled: false };
const TRIM_AT_ONCE = {
  enabled: true,
  trigger_pct: 2,
  target_floor_pct: 0,
  summary_budget_pct: 1,
  min_keep_pairs: 1,
  summary_model: FAKE_MODEL_ID,
};
// 沒有等 hook 的話早就到了；多留一點給慢機器，但要比卡住的 hook 短得多。
const NOT_WAITING_LIMIT_MS = HANG_MS / 2;
// 卡住的 hook 回了才有紀錄。
const HUNG_RUN_TIMEOUT_MS = HANG_MS + 15_000;
const NOTICE_TEXT = /管理員設定了 Hook/;

let adminToken = "";

async function toolRounds(): Promise<number> {
  return (await modelCalls()).filter((call) => call.lastRole === "tool").length;
}

/** 送出要叫工具的訊息；有審批卡就按批准，等工具結果回到模型、畫面閒置。 */
async function sendAndFinishTool(page: Page, text: string): Promise<void> {
  const before = await toolRounds();
  await sendMessage(page, text);
  await expect
    .poll(async () => {
      if (await approveButton(page).isVisible()) await approveButton(page).click();
      return toolRounds();
    })
    .toBe(before + 1);
  await composer(page).waitFor();
}

/**
 * 送出並等模型收到。剪裁先等畫面收尾的保存、被擋的保存接在剪過的存檔上（ADR-0302），
 * 所以不必先恢復。看內容不看次數：剪裁寫摘要也會呼叫模型。
 */
async function sendAndWaitForModel(page: Page, text: string): Promise<void> {
  const before = (await modelCalls()).length;
  await sendMessage(page, text);
  await expect.poll(async () => (await modelCalls()).slice(before)
    .some((call) => call.lastRole === "user" && call.lastText.includes(text))).toBe(true);
  await composer(page).waitFor();
}

/** 寫摘要的那次模型呼叫：送的是「[時間] user: …」的對話紀錄。 */
async function summaryCalls(): Promise<number> {
  return (await modelCalls()).filter((call) => /\] user: /.test(call.lastText)).length;
}

interface HeldSave { arrived: number; released?: number }

/** 拖慢畫面收尾的保存（最後一則是 AI 回覆的整棵保存）；記下每次扣住與放行的時間。 */
async function delayClosingSaves(page: Page, ms: number): Promise<HeldSave[]> {
  const saves: HeldSave[] = [];
  await page.route(SESSION_SAVE_URL, async (route) => {
    const body = route.request().method() === "POST" ? route.request().postDataJSON() : null;
    if (body?.messages?.messages?.[body.messages.currentId]?.role === "assistant") {
      const save: HeldSave = { arrived: Date.now() };
      saves.push(save);
      await new Promise((resolve) => setTimeout(resolve, ms));
      save.released = Date.now();
    }
    await route.continue();
  });
  return saves;
}

/** 設好家對話後一直送長訊息，直到聊天側剪裁存好（PostCompact 到）。 */
async function chatUntilTrimmed(page: Page): Promise<void> {
  await sendMessage(page, LONG_MESSAGE);
  await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
  await composer(page).waitFor();
  // 只有家對話才剪；換模型後的第一輪也不剪，所以多送幾則。加編號：寫摘要時前面的長訊息也會送給模型
  await setChatAsHome(page);
  for (let attempt = 0; attempt < MAX_TRIM_ATTEMPTS; attempt += 1) {
    if ((await receivedFor("PostCompact")).length > 0) return;
    await sendAndWaitForModel(page, `${LONG_MESSAGE}（第 ${attempt + 1} 則）`);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

/** 剪裁真的存好：最早那則長訊息不在存檔裡，摘要只叫了一次模型，畫面沒有保存提示，下一則直接送。 */
async function expectTrimmedAndChatting(page: Page, summariesBefore: number): Promise<void> {
  await expectBackgroundHook("PostCompact");
  const saved = await savedMessages(adminToken, await latestSessionId(adminToken));
  expect(saved.some((message) => message.content === LONG_MESSAGE)).toBe(false);
  expect(await summaryCalls()).toBe(summariesBefore + 1);
  await sendAndWaitForModel(page, "剪裁之後的訊息");
  await expectNoSaveNotice(page);
}

/** 走到設定頁，看執行紀錄裡有這些 hook。背景事件要等它跑完才有紀錄。 */
async function expectRunRows(
  page: Page,
  info: TestInfo,
  names: string[],
  timeout?: number,
): Promise<void> {
  await expect
    .poll(
      async () => {
        const runs = await hookRuns(adminToken);
        return names.every((name) => runs.some((run) => run.hook === name));
      },
      { timeout },
    )
    .toBe(true);
  await openChat(page); // 重新載入：設定頁開著時紀錄不會自己更新
  await openHooksTab(page);
  const rows = names.map((name) => page.getByRole("listitem").filter({ hasText: name }).first());
  for (const row of rows) await expect(row).toBeVisible();
  await shot(page, info, "runs", rows[0]);
}

// API 準備要排在瀏覽器登入之前：再登入會換掉瀏覽器的 refresh token。
test.beforeEach(async ({ page }) => {
  adminToken = await adminLogin();
  await disableHooks(adminToken);
  await resetRecorders();
  await loginInBrowser(page);
});

test("一輪回覆因錯誤結束：收到錯誤類型與遮過的內容，Stop 不跑", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "StopFailure", {
    url: hookUrl("ok"),
    name: "e2e-stop-failure",
    headers: `X-E2E-Canary: ${CANARY_HOOK_HEADER}`,
  });
  await addHttpHook(page, "Stop", { url: hookUrl("ok"), name: "e2e-stop" });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  await sendMessage(page, MODEL_400_REQUEST);
  const failure = await expectBackgroundHook("StopFailure");
  expect(failure.payload.error).toBe("invalid_request");
  expect(String(failure.payload.error_details)).toContain("400");
  await composer(page).waitFor();
  await expectLastReplyMarkedFailed(page);
  // 模型失敗時後端什麼都沒寫，畫面把失敗那則存好：不跳提示，下一則直接送（ADR-0231 決定 6）
  await expectNoSaveNotice(page);
  await shot(page, info, "model-400");

  // 模型回 200 但內容讀不懂：以前會變成 KeyError，現在是一般的模型失敗
  await sendMessage(page, MODEL_BAD_JSON_REQUEST);
  await expect.poll(async () => (await receivedFor("StopFailure")).length).toBe(2);
  // 上一則失敗的空白回覆送出前拿掉了：兩則 user 併成一則送到模型
  const merged = (await modelCalls()).at(-1)!;
  expect(merged.lastRole).toBe("user");
  expect(merged.lastText).toContain(MODEL_400_REQUEST);
  expect(merged.lastText).toContain(MODEL_BAD_JSON_REQUEST);
  const unreadable = (await receivedFor("StopFailure"))[1];
  expectCleanPayload("StopFailure", unreadable);
  expect(unreadable.payload.error).toBe("unknown");
  expect(String(unreadable.payload.error_details)).toContain("200");
  await composer(page).waitFor();
  await expectLastReplyMarkedFailed(page);
  await expect(page.getByText(/KeyError|TypeError|Traceback/)).toHaveCount(0);
  await shot(page, info, "model-bad-json");

  // 模型回 200 但沒有回覆（這裡只有錯誤本文）：以前是空白泡泡、算正常結束，Stop 還會跑
  await expectNoSaveNotice(page);
  await sendMessage(page, MODEL_NO_REPLY_REQUEST);
  await expect.poll(async () => (await receivedFor("StopFailure")).length).toBe(3);
  const noReply = (await receivedFor("StopFailure"))[2];
  expectCleanPayload("StopFailure", noReply);
  expect(noReply.payload.error).toBe("overloaded");
  await composer(page).waitFor();
  await expectLastReplyMarkedFailed(page);
  await shot(page, info, "model-no-reply");

  // 模型回 200、有回應的外殼但沒有內容，停止原因也不正常：這條才走到解析之後的判斷
  await expectNoSaveNotice(page);
  await sendMessage(page, MODEL_EMPTY_REPLY_REQUEST);
  await expect.poll(async () => (await receivedFor("StopFailure")).length).toBe(4);
  const emptyReply = (await receivedFor("StopFailure"))[3];
  expectCleanPayload("StopFailure", emptyReply);
  expect(emptyReply.payload.error).toBe("unknown");
  expect(String(emptyReply.payload.error_details)).toContain("insufficient_system_resource");
  await composer(page).waitFor();
  await expectLastReplyMarkedFailed(page);
  await shot(page, info, "model-empty-reply");

  // 不用按任何按鈕，標示已經進存檔。看存檔本身：重新載入會接回已結束的串流，畫面上的標示可能是重標的
  await expectNoSaveNotice(page);
  const saved = await savedMessages(adminToken, String(noReply.payload.session_id));
  expect(saved.filter((message) => message.generationFailed)).toHaveLength(4);
  await page.reload();
  await composer(page).waitFor();
  await expectLastReplyMarkedFailed(page);
  await shot(page, info, "still-marked-after-reload");

  expect(await receivedFor("Stop")).toHaveLength(0);
  await expectRunRows(page, info, ["e2e-stop-failure"]);
});

test("子代理：補充說明進第一則訊息，內容不含目標，結束時收到回覆", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "SubagentStart", {
    url: hookUrl("subagent_context"),
    name: "e2e-sub-start",
    matcher: "subagent",
  });
  await addHttpHook(page, "SubagentStop", { url: hookUrl("ok"), name: "e2e-sub-stop", matcher: "subagent" });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  await sendAndFinishTool(page, subagentRequest());
  await shot(page, info, "dispatched");

  const start = await expectBackgroundHook("SubagentStart", null);
  expect(start.payload.agent_type).toBe("subagent");
  expect(start.payload.agent_id).not.toBe(SOUL_ID);
  expect(JSON.stringify(start.payload)).not.toContain(SUBAGENT_GOAL);

  const stop = await expectBackgroundHook("SubagentStop", null);
  expect(stop.payload.agent_id).toBe(start.payload.agent_id);
  expect(stop.payload.last_assistant_message).toBe(FAKE_TEXT_REPLY);

  const first = (await modelCalls()).find((call) => call.lastText.includes(SUBAGENT_GOAL));
  expect(first?.lastRole).toBe("user");
  expect(first?.lastText).toContain("hook_output");
  expect(first?.lastText).toContain(SUBAGENT_CONTEXT);
  await expectRunRows(page, info, ["e2e-sub-start", "e2e-sub-stop"]);
});

test("任務看板：建立時每個任務各一次，完成時一次，進行中不算", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "TaskCreated", { url: hookUrl("ok"), name: "e2e-task-created" });
  await addHttpHook(page, "TaskCompleted", { url: hookUrl("ok"), name: "e2e-task-completed" });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  await sendAndFinishTool(page, missionRequest());
  await expect.poll(async () => (await receivedFor("TaskCreated")).length).toBe(2);
  const created = await receivedFor("TaskCreated");
  for (const hook of created) expectCleanPayload("TaskCreated", hook);
  const subjects = Object.fromEntries(created.map((hook) => [hook.payload.task_id, hook.payload.task_subject]));
  expect(subjects).toEqual(TASK_SUBJECTS);

  await sendAndFinishTool(page, taskRequest("DOING", "T1"));
  await sendAndFinishTool(page, taskRequest("DONE", "T1"));
  await shot(page, info, "done");
  const completed = await expectBackgroundHook("TaskCompleted");
  expect(completed.payload.task_id).toBe("T1");
  expect(completed.payload.mission_id).toBe(created[0].payload.mission_id);
  await new Promise((resolve) => setTimeout(resolve, 500));
  expect(await receivedFor("TaskCompleted")).toHaveLength(1);
  await expectRunRows(page, info, ["e2e-task-created", "e2e-task-completed"]);
});

test("剪裁（聊天）：PreCompact 卡住時聊天不受影響，剪裁真的存好、下一則不用恢復", async ({ page }, info) => {
  await setSlidingWindow(adminToken, TRIM_AT_ONCE);
  try {
    await openHooksTab(page);
    await enableHooks(page);
    await addHttpHook(page, "PreCompact", { url: hookUrl("hang"), name: "e2e-pre-compact", matcher: "auto" });
    await addHttpHook(page, "PostCompact", { url: hookUrl("ok"), name: "e2e-chat-post", matcher: "auto" });
    await saveHooks(page);

    await openChat(page);
    await selectSoul(page);
    const summaries = await summaryCalls();
    await chatUntilTrimmed(page);
    const pre = await expectBackgroundHook("PreCompact");
    expect(pre.payload.trigger).toBe("auto");

    // PreCompact 還卡著，聊天照常；上方的舊訊息在下一則存好後從畫面消失（Owner 11/06 同意）
    await expectTrimmedAndChatting(page, summaries);
    expect(Date.now() - pre.receivedAt).toBeLessThan(NOT_WAITING_LIMIT_MS);
    await shot(page, info, "chat-after-trim");
    await expectRunRows(page, info, ["e2e-pre-compact"], HUNG_RUN_TIMEOUT_MS);
  } finally {
    await setSlidingWindow(adminToken, SLIDING_WINDOW_OFF);
  }
});

for (const [name, delayMs, waits] of [
  ["在上限內：剪裁等它存好再剪", 3_000, true],
  ["超過上限：剪裁先剪文字版，畫面接上去", CLOSING_SAVE_WAIT_MS + 3_000, false],
] as const) {
  test(`剪裁（聊天）：收尾保存拖慢${name}，不跳提示`, async ({ page }, info) => {
    await setSlidingWindow(adminToken, TRIM_AT_ONCE);
    try {
      await openHooksTab(page);
      await enableHooks(page);
      await addHttpHook(page, "PostCompact", { url: hookUrl("ok"), name: "e2e-slow-save-post", matcher: "auto" });
      await saveHooks(page);

      await openChat(page);
      await selectSoul(page);
      const saves = await delayClosingSaves(page, delayMs);
      const summaries = await summaryCalls();
      await chatUntilTrimmed(page);
      const post = await expectBackgroundHook("PostCompact");
      // 等到了：存回時沒有收尾保存還扣著。逾時：那一輪的收尾保存還扣著就先存回了
      const heldAtCommit = saves.some((save) => save.arrived < post.receivedAt &&
        (save.released ?? Number.POSITIVE_INFINITY) > post.receivedAt);
      expect(heldAtCommit).toBe(!waits);
      await expect.poll(() => saves.every((save) => save.released), { timeout: delayMs + 10_000 }).toBe(true);
      await expectTrimmedAndChatting(page, summaries);
      await shot(page, info, waits ? "slow-save-within-limit" : "slow-save-past-limit");
    } finally {
      await page.unroute(SESSION_SAVE_URL);
      await setSlidingWindow(adminToken, SLIDING_WINDOW_OFF);
    }
  });
}

// PostCompact 走心跳這條：沒有畫面補存，也照樣存好。
test("剪裁（心跳）：PreCompact 卡住時 PostCompact 照樣到，內容有摘要", async ({ page }, info) => {
  await setSlidingWindow(adminToken, TRIM_AT_ONCE);
  try {
    await openHooksTab(page);
    await enableHooks(page);
    await addHttpHook(page, "PreCompact", { url: hookUrl("hang"), name: "e2e-beat-pre", matcher: "auto" });
    await addHttpHook(page, "PostCompact", { url: hookUrl("ok"), name: "e2e-beat-post", matcher: "auto" });
    await addHttpHook(page, "HeartbeatStop", { url: hookUrl("ok"), name: "e2e-beat-done" });
    await saveHooks(page);

    // 換模型後的第一輪不剪，保留一組之外才有東西可剪，所以多跑幾次
    for (let attempt = 0; attempt < MAX_TRIM_ATTEMPTS; attempt += 1) {
      if ((await receivedFor("PostCompact")).length > 0) break;
      const done = (await receivedFor("HeartbeatStop")).length;
      await awakenSoul(LONG_MESSAGE, "home");
      await expect.poll(async () => (await receivedFor("HeartbeatStop")).length).toBeGreaterThan(done);
    }
    const pre = await expectBackgroundHook("PreCompact");
    const post = await expectBackgroundHook("PostCompact");
    expect(post.receivedAt - pre.receivedAt).toBeLessThan(NOT_WAITING_LIMIT_MS);
    expect(post.payload.trigger).toBe("auto");
    expect(post.payload.session_id).toBe(pre.payload.session_id);
    expect(String(post.payload.compact_summary)).toContain(FAKE_TEXT_REPLY);
    await expectRunRows(page, info, ["e2e-beat-post"]);
  } finally {
    await setSlidingWindow(adminToken, SLIDING_WINDOW_OFF);
  }
});

test("設定存好才通知：回擋下照樣存好，內容沒有設定值，關閉 hooks 那次不觸發", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "ConfigChange", { url: hookUrl("block_prompt"), name: "e2e-config", matcher: "hooks" });
  await addHttpHook(page, "ConfigChange", {
    url: hookUrl("ok"),
    name: "e2e-config-window",
    matcher: "sliding_window",
  });
  await saveHooks(page);

  // 這次存檔本身就是一次設定變更；比對規則只讓第一個 hook 跑
  const hooksChange = await expectBackgroundHook("ConfigChange", null);
  expect(hooksChange.payload.source).toBe("hooks");
  expect(hooksChange.payload.session_id).toBeNull();
  expect(await hooksEnabled(adminToken)).toBe(true);
  await openChat(page);
  await openHooksTab(page);
  await expect(
    eventSection(page, "ConfigChange").getByRole("textbox", { name: "名稱（選填）" }).first(),
  ).toHaveValue("e2e-config");
  await shot(page, info, "still-saved", eventSection(page, "ConfigChange"));

  await setSlidingWindow(adminToken, { ...SLIDING_WINDOW_OFF, summary_model: CANARY_API_KEY });
  await expect.poll(async () => (await receivedFor("ConfigChange")).length).toBe(2);
  const windowChange = (await receivedFor("ConfigChange"))[1];
  expectCleanPayload("ConfigChange", windowChange, null);
  expect(windowChange.payload.source).toBe("sliding_window");
  await setSlidingWindow(adminToken, SLIDING_WINDOW_OFF);
  await expect.poll(async () => (await receivedFor("ConfigChange")).length).toBe(3);

  await expectRunRows(page, info, ["e2e-config", "e2e-config-window"]);

  await enableHooks(page);
  await saveHooks(page);
  expect(await hooksEnabled(adminToken)).toBe(false);
  await new Promise((resolve) => setTimeout(resolve, 500));
  expect(await receivedFor("ConfigChange")).toHaveLength(3);
});

test("心跳：開始與結束都收到，內容有脈衝數與結束原因", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "HeartbeatStart", { url: hookUrl("ok"), name: "e2e-beat-start" });
  await addHttpHook(page, "HeartbeatStop", { url: hookUrl("ok"), name: "e2e-beat-stop" });
  await saveHooks(page);

  await awakenSoul("實際驗證：心跳");
  const start = await expectBackgroundHook("HeartbeatStart");
  expect(start.payload.pulses_total).toBe(1);
  expect(start.payload.home).toBe(false);
  const stop = await expectBackgroundHook("HeartbeatStop");
  expect(stop.payload.pulses_total).toBe(1);
  expect(stop.payload.pulses_done).toBe(1);
  expect(stop.payload.end).toBe("completed");
  expect(stop.payload.session_id).toBe(start.payload.session_id);
  await expectRunRows(page, info, ["e2e-beat-start", "e2e-beat-stop"]);
});

test("心跳的模型失敗：StopFailure 有發，結束原因是模型失敗，剩下的脈衝不跑", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "StopFailure", { url: hookUrl("ok"), name: "e2e-beat-failure" });
  await addHttpHook(page, "HeartbeatStop", { url: hookUrl("ok"), name: "e2e-beat-end" });
  await saveHooks(page);

  await awakenSoul(MODEL_400_REQUEST, "independent", 3);
  const failure = await expectBackgroundHook("StopFailure");
  expect(failure.payload.error).toBe("invalid_request");
  const stop = await expectBackgroundHook("HeartbeatStop");
  expect(stop.payload.end).toBe("model_error");
  expect(stop.payload.pulses_total).toBe(3);
  expect(stop.payload.pulses_done).toBe(1);
  await expectRunRows(page, info, ["e2e-beat-failure", "e2e-beat-end"]);
});

test("MCP 要資料：hook 回代填時表單照樣跳，答案是人填的，也發通知", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "Elicitation", {
    url: hookUrl("elicit_accept"),
    name: "e2e-elicit",
    matcher: MCP_SERVER,
  });
  await addHttpHook(page, "Notification", {
    url: hookUrl("ok"),
    name: "e2e-elicit-notice",
    matcher: "elicitation_dialog",
  });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  await sendMessage(page, mcpRequest());
  const submit = page.getByRole("button", { name: "送出並繼續" });
  await expect
    .poll(async () => {
      if (await approveButton(page).isVisible()) await approveButton(page).click();
      return submit.isVisible();
    })
    .toBe(true);
  await expect(page.getByText(MCP_FORM_MESSAGE).first()).toBeVisible();
  await shot(page, info, "form-still-shows");

  const [elicitation] = await receivedFor("Elicitation");
  expectCleanPayload("Elicitation", elicitation);
  expect(elicitation.payload.mcp_server_name).toBe(MCP_SERVER);
  expect(elicitation.payload.message).toBe(MCP_FORM_MESSAGE);
  expect(elicitation.payload.mode).toBe("form");
  const notice = await expectBackgroundHook("Notification");
  expect(notice.payload.notification_type).toBe("elicitation_dialog");
  expect(JSON.stringify(notice.payload)).not.toContain(MCP_FORM_MESSAGE);

  await page.getByRole("textbox", { name: "答案" }).fill(PERSON_FORM_ANSWER);
  await submit.click();
  await expect.poll(toolRounds).toBe(1);
  await composer(page).waitFor();
  const forModel = await lastToolResultForModel();
  expect(forModel).toContain(PERSON_FORM_ANSWER);
  expect(forModel).not.toContain(HOOK_FORM_ANSWER);
  const run = (await hookRuns(adminToken)).find((row) => row.hook === "e2e-elicit");
  expect(run?.outcome).toBe("none");
  expect(run?.detail).toContain("hooks can not answer for the user");
  await expectRunRows(page, info, ["e2e-elicit", "e2e-elicit-notice"]);
});

test("MCP 要資料：hook 拒絕時不跳表單，也不發通知", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "Elicitation", {
    url: hookUrl("elicit_decline"),
    name: "e2e-elicit-decline",
    matcher: MCP_SERVER,
  });
  await addHttpHook(page, "Notification", {
    url: hookUrl("ok"),
    name: "e2e-elicit-notice",
    matcher: "elicitation_dialog",
  });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  await sendAndFinishTool(page, mcpRequest());
  await shot(page, info, "no-form");
  await expect(page.getByRole("button", { name: "送出並繼續" })).toHaveCount(0);
  expect(await lastToolResultForModel()).toContain("decline");
  expect(await receivedFor("Notification")).toHaveLength(0);
  await expectRunRows(page, info, ["e2e-elicit-decline"]);
});

test("無痕提示：有會收到對話內容的 hook 才顯示", async ({ page }, info) => {
  const notice = page.getByRole("status").filter({ hasText: NOTICE_TEXT });
  async function openIncognitoAndWaitForCheck(): Promise<void> {
    await openChat(page);
    const checked = page.waitForResponse((response) => response.url().endsWith("/v1/hooks/active"));
    await page.getByRole("button", { name: "無痕對話" }).click();
    await checked;
  }

  // 沒有 hook：不提示
  await openIncognitoAndWaitForCheck();
  await page.waitForTimeout(300);
  await expect(notice).toHaveCount(0);
  await shot(page, info, "no-hook");

  // 只有不帶對話內容的 hook：不提示
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "ConfigChange", { url: hookUrl("ok"), name: "e2e-only-config" });
  await saveHooks(page);
  await openIncognitoAndWaitForCheck();
  await page.waitForTimeout(300);
  await expect(notice).toHaveCount(0);

  // 有帶對話內容的 hook：提示
  await openHooksTab(page);
  await addHttpHook(page, "UserPromptSubmit", { url: hookUrl("ok"), name: "e2e-prompt" });
  await saveHooks(page);
  await openIncognitoAndWaitForCheck();
  await expect(notice).toBeVisible();
  await shot(page, info, "notice");

  // 一般對話不提示
  await openChat(page);
  await expect(notice).toHaveCount(0);
});
