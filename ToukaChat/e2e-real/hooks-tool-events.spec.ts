import { expect, test, type Page } from "@playwright/test";
import { adminLogin, disableHooks, hookRuns } from "./lib/api";
import {
  ASK_ANSWER,
  ASK_QUESTION,
  CANARY_API_KEY,
  CANARY_HOOK_HEADER,
  ENV_SANDBOX_ROOT,
  FAKE_AFTER_TOOL_REPLY,
  SOUL_ID,
  askRequest,
  badWriteRequest,
  readRequest,
  writeRequest,
} from "./lib/constants";
import { modelCalls, receivedFor, resetRecorders, sandboxHasFile } from "./lib/control";
import { DENY_REASON, FAILURE_CONTEXT, HOOK_REWRITTEN_FILE, hookUrl, type ReceivedHook } from "./lib/hook_sink";
import {
  addHttpHook,
  composer,
  enableHooks,
  loginInBrowser,
  openChat,
  openHooksTab,
  saveHooks,
  selectSoul,
  sendMessage,
  shot,
} from "./lib/ui";

const SHARED_KEYS = ["agent_id", "hook_event_name", "incognito", "permission_mode", "session_id"];
const TOOL_KEYS = ["tool_input", "tool_name"];
// 事件內容的鍵（白名單）：多了就可能把設定值帶出去。
const EXPECTED_KEYS: Record<string, string[]> = {
  PostToolUseFailure: [...SHARED_KEYS, ...TOOL_KEYS, "error", "tool_use_id"].sort(),
  PermissionRequest: [...SHARED_KEYS, ...TOOL_KEYS].sort(),
  PermissionDenied: [...SHARED_KEYS, ...TOOL_KEYS, "reason", "tool_use_id"].sort(),
  Notification: [...SHARED_KEYS, "message", "notification_type"].sort(),
};

let adminToken = "";

function expectCleanPayload(event: string, hook: ReceivedHook | undefined): asserts hook is ReceivedHook {
  expect(hook, event).toBeDefined();
  if (!hook) return;
  expect(Object.keys(hook.payload).sort(), event).toEqual(EXPECTED_KEYS[event]);
  expect(hook.payload.agent_id, event).toBe(SOUL_ID);
  const body = JSON.stringify(hook.payload);
  expect(body, event).not.toContain(CANARY_API_KEY);
  expect(body, event).not.toContain(CANARY_HOOK_HEADER);
}

/** 背景事件不等回覆，要等它真的到了才核對。 */
async function expectBackgroundHook(event: string): Promise<ReceivedHook> {
  await expect.poll(async () => (await receivedFor(event)).length, event).toBeGreaterThan(0);
  const [hook] = await receivedFor(event);
  expectCleanPayload(event, hook);
  return hook;
}

function approveButton(page: Page) {
  return page.getByRole("button", { name: "批准", exact: true });
}

async function lastToolResultForModel(): Promise<string> {
  const toolCalls = (await modelCalls()).filter((call) => call.lastRole === "tool");
  return toolCalls.at(-1)?.lastText ?? "";
}

// API 準備要排在瀏覽器登入之前：再登入會換掉瀏覽器的 refresh token。
test.beforeEach(async ({ page }) => {
  adminToken = await adminLogin();
  await disableHooks(adminToken);
  await resetRecorders();
  await loginInBrowser(page);
});

test("審批卡前的 hook 回放行時卡照樣跳，參數不被改，也發通知", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "PermissionRequest", {
    url: hookUrl("allow_permission"),
    name: "e2e-allow-card",
    matcher: "write",
    headers: `X-E2E-Canary: ${CANARY_HOOK_HEADER}`,
  });
  await addHttpHook(page, "Notification", {
    url: hookUrl("ok"),
    name: "e2e-notify",
    matcher: "permission_prompt",
    headers: `X-E2E-Canary: ${CANARY_HOOK_HEADER}`,
  });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  const fileName = "e2e-card-still-shows.txt";
  await sendMessage(page, writeRequest(fileName));
  await expect(approveButton(page)).toBeVisible();
  await shot(page, info, "card");

  const [request] = await receivedFor("PermissionRequest");
  expectCleanPayload("PermissionRequest", request);
  expect(request.payload.tool_name).toBe("write");
  const notice = await expectBackgroundHook("Notification");
  expect(notice.payload.notification_type).toBe("permission_prompt");
  expect(notice.payload.message).toContain("write");
  // 人還沒按，工具不能先跑
  expect(sandboxHasFile(fileName)).toBe(false);
  expect(sandboxHasFile(HOOK_REWRITTEN_FILE)).toBe(false);

  await approveButton(page).click();
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  expect(sandboxHasFile(fileName)).toBe(true);
  expect(sandboxHasFile(HOOK_REWRITTEN_FILE)).toBe(false);
  const run = (await hookRuns(adminToken)).find((row) => row.hook === "e2e-allow-card");
  expect(run?.outcome).toBe("none");
  expect(run?.detail).toContain("hooks only deny");
  expect(run?.detail).toContain("decision.updatedInput");
});

test("審批卡前的 hook 拒絕時不跳卡、不執行，被拒事件收到固定代碼", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "PermissionRequest", {
    url: hookUrl("deny_permission"),
    name: "e2e-deny-card",
    matcher: "write",
  });
  await addHttpHook(page, "Notification", { url: hookUrl("ok"), name: "e2e-notify" });
  await addHttpHook(page, "PermissionDenied", { url: hookUrl("ok"), name: "e2e-denied", matcher: "write" });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  const fileName = "e2e-denied-by-hook.txt";
  await sendMessage(page, writeRequest(fileName));
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  await composer(page).waitFor();
  await expect(page.getByText(/hook 拒絕/).first()).toBeVisible();
  await shot(page, info, "no-card");
  await expect(approveButton(page)).toHaveCount(0);
  expect(sandboxHasFile(fileName)).toBe(false);

  const denied = await expectBackgroundHook("PermissionDenied");
  expect(denied.payload.reason).toBe("hook_denied");
  expect(await receivedFor("Notification")).toHaveLength(0);
  const forModel = await lastToolResultForModel();
  expect(forModel).toContain("hook_denied");
  expect(forModel).toContain(DENY_REASON);
});

test("人按拒絕時被拒事件收到固定代碼；通知只比對通知類型", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "PermissionDenied", { url: hookUrl("ok"), name: "e2e-denied", matcher: "write" });
  await addHttpHook(page, "Notification", {
    url: hookUrl("ok"),
    name: "e2e-other-type",
    matcher: "agent_needs_input",
  });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  const fileName = "e2e-rejected-by-person.txt";
  await sendMessage(page, writeRequest(fileName));
  await page.getByRole("button", { name: "拒絕", exact: true }).click();
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  await composer(page).waitFor();
  await shot(page, info, "rejected");
  expect(sandboxHasFile(fileName)).toBe(false);

  const denied = await expectBackgroundHook("PermissionDenied");
  expect(denied.payload.reason).toBe("approval_rejected");
  expect(await receivedFor("Notification")).toHaveLength(0);
});

test("工具跑了才失敗：hook 收到錯誤內容，補充說明回到模型，成功事件不觸發", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "PostToolUseFailure", {
    url: hookUrl("failure_context"),
    name: "e2e-failure",
    matcher: "read",
  });
  await addHttpHook(page, "PostToolUse", { url: hookUrl("ok"), name: "e2e-success", matcher: "read" });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  await sendMessage(page, readRequest("e2e-no-such-file.txt"));
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  await composer(page).waitFor();
  await shot(page, info, "failed-tool");

  const [failure] = await receivedFor("PostToolUseFailure");
  expectCleanPayload("PostToolUseFailure", failure);
  expect(failure.payload.tool_name).toBe("read");
  // 這條路徑的錯誤本來就沒有路徑，遮罩由 test_hook_tool_events_security 驗
  const errorText = String(failure.payload.error);
  expect(errorText.length).toBeGreaterThan(0);
  expect(errorText).not.toContain(process.env[ENV_SANDBOX_ROOT] ?? "<unset>");
  expect(await receivedFor("PostToolUse")).toHaveLength(0);

  const forModel = await lastToolResultForModel();
  expect(forModel).toContain("hook_output");
  expect(forModel).toContain(FAILURE_CONTEXT);
});

test("代理人問問題：通知收到，答完繼續", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "Notification", { url: hookUrl("ok"), name: "e2e-question", matcher: "agent_needs_input" });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  await sendMessage(page, askRequest());
  await expect(page.getByText(ASK_QUESTION).first()).toBeVisible();
  await shot(page, info, "question");

  const notice = await expectBackgroundHook("Notification");
  expect(notice.payload.notification_type).toBe("agent_needs_input");
  expect(JSON.stringify(notice.payload)).not.toContain(ASK_QUESTION);

  await page.getByText(ASK_ANSWER, { exact: true }).click();
  await page.getByRole("button", { name: "送出回答" }).click();
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  expect(await lastToolResultForModel()).toContain(ASK_ANSWER);
});

test("工具缺必要參數：回一般的參數錯誤，不算工具失敗", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "PostToolUseFailure", { url: hookUrl("ok"), name: "e2e-failure" });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  const fileName = "e2e-missing-content.txt";
  await sendMessage(page, badWriteRequest(fileName));
  await approveButton(page).click();
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  await composer(page).waitFor();
  await page.getByText(/已調用/).last().click();
  await expect(page.getByText(/missing required parameters: content/).first()).toBeVisible();
  await shot(page, info, "validation-error");

  await expect(page.getByText("TypeError")).toHaveCount(0);
  expect(sandboxHasFile(fileName)).toBe(false);
  const forModel = await lastToolResultForModel();
  expect(forModel).toContain("validation_error");
  expect(forModel).toContain("content");
  expect(await receivedFor("PostToolUseFailure")).toHaveLength(0);
});

test("審批卡前的 hook 自己失敗時卡照樣跳，紀錄看得到失敗", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "PermissionRequest", { url: hookUrl("fail"), name: "e2e-card-fail", matcher: "write" });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  const fileName = "e2e-hook-failed.txt";
  await sendMessage(page, writeRequest(fileName));
  await expect(approveButton(page)).toBeVisible();
  await approveButton(page).click();
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  expect(sandboxHasFile(fileName)).toBe(true);

  await openHooksTab(page);
  const row = page.getByRole("listitem").filter({ hasText: "e2e-card-fail" }).first();
  await expect(row).toContainText("hook 失敗（不擋）");
  await shot(page, info, "runs", row);
});

test("無痕對話：審批相關事件照跑，紀錄只有時間、事件、結果", async ({ page }, info) => {
  const names = ["e2e-incognito-request", "e2e-incognito-notice", "e2e-incognito-denied"];
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "PermissionRequest", { url: hookUrl("ok"), name: names[0] });
  await addHttpHook(page, "Notification", { url: hookUrl("ok"), name: names[1] });
  await addHttpHook(page, "PermissionDenied", { url: hookUrl("ok"), name: names[2] });
  await saveHooks(page);

  await openChat(page);
  // 按「無痕對話」會開新對話並換回預設模型，所以靈魂要在這之後選
  await page.getByRole("button", { name: "無痕對話" }).click();
  await selectSoul(page);
  await sendMessage(page, writeRequest("e2e-incognito.txt"));
  await page.getByRole("button", { name: "拒絕", exact: true }).click();
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();

  for (const event of ["PermissionRequest", "Notification", "PermissionDenied"]) {
    await expect.poll(async () => (await receivedFor(event)).length, event).toBe(1);
    const [hook] = await receivedFor(event);
    expect(hook.payload.incognito, event).toBe(true);
  }
  await expect
    .poll(async () => (await hookRuns(adminToken)).filter((run) => run.incognito).length)
    .toBeGreaterThanOrEqual(3);
  for (const run of (await hookRuns(adminToken)).filter((row) => row.incognito)) {
    expect(run.hook).toBe("");
    expect(run.detail).toBeNull();
  }

  await openHooksTab(page);
  const incognitoRow = page.getByText("（無痕）").first();
  await expect(incognitoRow).toBeVisible();
  for (const name of names) {
    await expect(page.getByRole("listitem").filter({ hasText: name })).toHaveCount(0);
  }
  await shot(page, info, "runs", incognitoRow);
});
