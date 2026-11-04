import { expect, test, type Page, type TestInfo } from "@playwright/test";
import {
  adminLogin,
  awakenSoul,
  disableHooks,
  hookRuns,
  hooksEnabled,
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
  loginInBrowser,
  openChat,
  openHooksTab,
  saveHooks,
  selectSoul,
  sendMessage,
  shot,
} from "./lib/ui";

const PERSON_FORM_ANSWER = "人填的答案";
// 未知模型的 context window 是 128000，trigger_pct 2 就是 2560 token；每則訊息要明顯超過。
const LONG_MESSAGE = "實際驗證剪裁用的長訊息。".repeat(1200);
const MAX_TRIM_ATTEMPTS = 4;
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

/** 保存狀態不確定或對不上時，人要走的路：先核對，再重試保存（ADR-0231）。 */
async function recoverSave(page: Page): Promise<void> {
  await page.getByRole("button", { name: "重新核對保存結果" }).click();
  await page.getByRole("button", { name: "僅重試保存" }).click();
  await expect(page.getByRole("status").filter({ hasText: /保存/ })).toHaveCount(0);
}

/**
 * 送出並等模型收到。設為家對話會換掉對話版本，畫面手上的版本過期、下一則存不進去
 * （既有問題，見 ADR-0295 的「實際驗證找到的既有問題」），這時恢復後再送一次。
 */
async function sendRecovering(page: Page, text: string): Promise<void> {
  const before = (await modelCalls()).length;
  await sendMessage(page, text);
  await expect
    .poll(async () => {
      if (await page.getByRole("button", { name: "重新核對保存結果" }).isVisible()) {
        await recoverSave(page);
        await sendMessage(page, text);
      }
      return (await modelCalls()).length;
    })
    .toBeGreaterThan(before);
  await composer(page).waitFor();
}

async function setCurrentChatAsHome(page: Page): Promise<void> {
  await page.getByRole("button", { name: /^會話選項：/ }).first().click();
  await page.getByRole("menuitem", { name: "設為家對話" }).click();
  await page.keyboard.press("Escape");
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
  await shot(page, info, "model-400");

  // 模型失敗後對話的保存狀態不確定，要先恢復才送得出下一則
  await recoverSave(page);

  // 模型回 200 但內容讀不懂：以前會變成 KeyError，現在是一般的模型失敗
  await sendMessage(page, MODEL_BAD_JSON_REQUEST);
  await expect.poll(async () => (await receivedFor("StopFailure")).length).toBe(2);
  const unreadable = (await receivedFor("StopFailure"))[1];
  expectCleanPayload("StopFailure", unreadable);
  expect(unreadable.payload.error).toBe("unknown");
  expect(String(unreadable.payload.error_details)).toContain("200");
  await composer(page).waitFor();
  await expect(page.getByText(/KeyError|TypeError|Traceback/)).toHaveCount(0);
  await shot(page, info, "model-bad-json");

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

test("剪裁（聊天）：PreCompact 卡住時聊天不受影響", async ({ page }, info) => {
  await setSlidingWindow(adminToken, TRIM_AT_ONCE);
  try {
    await openHooksTab(page);
    await enableHooks(page);
    await addHttpHook(page, "PreCompact", { url: hookUrl("hang"), name: "e2e-pre-compact", matcher: "auto" });
    await saveHooks(page);

    await openChat(page);
    await selectSoul(page);
    await sendMessage(page, LONG_MESSAGE);
    await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
    await composer(page).waitFor();
    // 只有家對話才剪；換模型後的第一輪也不剪，所以多送幾則
    await setCurrentChatAsHome(page);
    for (let attempt = 0; attempt < MAX_TRIM_ATTEMPTS; attempt += 1) {
      if ((await receivedFor("PreCompact")).length > 0) break;
      await sendRecovering(page, LONG_MESSAGE);
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    const pre = await expectBackgroundHook("PreCompact");
    expect(pre.payload.trigger).toBe("auto");

    // PreCompact 還卡著，聊天照常
    await sendRecovering(page, "剪裁之後的訊息");
    expect(Date.now() - pre.receivedAt).toBeLessThan(NOT_WAITING_LIMIT_MS);
    await shot(page, info, "chat-after-trim");
    await expectRunRows(page, info, ["e2e-pre-compact"], HUNG_RUN_TIMEOUT_MS);
  } finally {
    await setSlidingWindow(adminToken, SLIDING_WINDOW_OFF);
  }
});

// 聊天側剪裁和畫面收尾的保存會搶同一個版本，常常存不進去（見 ADR-0295 的「實際驗證找到的既有問題」），
// PostCompact 走心跳這條：沒有畫面搶版本，每次都會存好。
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
  await shot(page, info, "still-saved");

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
