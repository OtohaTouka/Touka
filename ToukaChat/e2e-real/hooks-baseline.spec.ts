import { expect, test } from "@playwright/test";
import { adminLogin, disableHooks, hookRuns } from "./lib/api";
import {
  CANARY_API_KEY,
  CANARY_HOOK_HEADER,
  FAKE_AFTER_TOOL_REPLY,
  FAKE_TEXT_REPLY,
  SOUL_ID,
  writeRequest,
} from "./lib/constants";
import { modelCalls, receivedFor, receivedHooks, resetRecorders, sandboxHasFile } from "./lib/control";
import { BLOCK_REASON, hookUrl } from "./lib/hook_sink";
import {
  addHttpHook,
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

const FOUR_EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"];
// 事件內容的鍵（白名單）：多了就可能把設定值帶出去。
const EXPECTED_KEYS: Record<string, string[]> = {
  UserPromptSubmit: ["agent_id", "hook_event_name", "incognito", "prompt", "session_id"],
  PreToolUse: [
    "agent_id", "hook_event_name", "incognito", "permission_mode", "session_id",
    "tool_input", "tool_name", "tool_use_id",
  ],
  PostToolUse: [
    "agent_id", "hook_event_name", "incognito", "permission_mode", "session_id",
    "tool_input", "tool_name", "tool_response", "tool_use_id",
  ],
  Stop: [
    "agent_id", "hook_event_name", "incognito", "last_assistant_message",
    "permission_mode", "session_id", "stop_hook_active",
  ],
};

let adminToken = "";

// API 準備要排在瀏覽器登入之前：再登入會換掉瀏覽器的 refresh token。
test.beforeEach(async ({ page }) => {
  adminToken = await adminLogin();
  await disableHooks(adminToken);
  await resetRecorders();
  await loginInBrowser(page);
});

test("一般對話：假模型的回覆顯示在畫面上", async ({ page }, info) => {
  await sendMessage(page, "你好，這是實際驗證");
  await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
  await shot(page, info, "reply");
});

test("四個事件都走到底：設定、觸發、收到內容、紀錄看得到", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  for (const event of FOUR_EVENTS) {
    await addHttpHook(page, event, {
      url: hookUrl("ok"),
      name: `e2e-${event}`,
      headers: `X-E2E-Canary: ${CANARY_HOOK_HEADER}`,
      ...(event.endsWith("ToolUse") ? { matcher: "write" } : {}),
    });
  }
  await shot(page, info, "settings", eventSection(page, "UserPromptSubmit"));
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  const fileName = "e2e-four-events.txt";
  await sendMessage(page, writeRequest(fileName));
  await page.getByRole("button", { name: "批准", exact: true }).click();
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  await shot(page, info, "after-approve");
  expect(sandboxHasFile(fileName)).toBe(true);

  await expect.poll(async () => (await receivedFor("Stop")).length).toBe(1);
  for (const event of FOUR_EVENTS) {
    const [hook] = await receivedFor(event);
    expect(hook, event).toBeDefined();
    expect(Object.keys(hook.payload).sort(), event).toEqual(EXPECTED_KEYS[event]);
    expect(hook.payload.agent_id, event).toBe(SOUL_ID);
    expect(hook.headers["x-e2e-canary"], event).toBe(CANARY_HOOK_HEADER);
    // 模型連線的金鑰與 hook 的標頭值都不能出現在事件內容裡
    const body = JSON.stringify(hook.payload);
    expect(body, event).not.toContain(CANARY_API_KEY);
    expect(body, event).not.toContain(CANARY_HOOK_HEADER);
  }
  const [pre] = await receivedFor("PreToolUse");
  expect(pre.payload.tool_name).toBe("write");
  const [prompt] = await receivedFor("UserPromptSubmit");
  expect(prompt.payload.prompt).toContain(fileName);
  const [stop] = await receivedFor("Stop");
  expect(stop.payload.last_assistant_message).toContain(FAKE_AFTER_TOOL_REPLY);

  await openHooksTab(page);
  const runRow = (event: string) => page.getByRole("listitem").filter({ hasText: `e2e-${event}` }).first();
  for (const event of FOUR_EVENTS) {
    await expect(runRow(event)).toContainText("HTTP 200");
    await expect(runRow(event)).toContainText("沒有動作");
  }
  await shot(page, info, "runs", runRow("UserPromptSubmit"));
});

test("hook 回放行時審批卡照樣跳，按拒絕工具不執行", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "PreToolUse", { url: hookUrl("allow"), name: "e2e-allow", matcher: "write" });
  await addHttpHook(page, "PostToolUse", { url: hookUrl("ok"), name: "e2e-post", matcher: "write" });
  await saveHooks(page);

  await openChat(page);
  await selectSoul(page);
  const fileName = "e2e-rejected.txt";
  await sendMessage(page, writeRequest(fileName));
  const approve = page.getByRole("button", { name: "批准", exact: true });
  await expect(approve).toBeVisible();
  expect(await receivedFor("PreToolUse")).toHaveLength(1);
  await shot(page, info, "card");

  await page.getByRole("button", { name: "拒絕", exact: true }).click();
  await expect(page.getByText(FAKE_AFTER_TOOL_REPLY).first()).toBeVisible();
  expect(await receivedFor("PostToolUse")).toHaveLength(0);
  expect(sandboxHasFile(fileName)).toBe(false);
  const allowRun = (await hookRuns(adminToken)).find((run) => run.hook === "e2e-allow");
  expect(allowRun?.outcome).toBe("none");
  expect(allowRun?.detail).toContain("hooks only deny or ask");
});

test("hook 自己失敗時不擋動作，執行紀錄看得到失敗", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "UserPromptSubmit", { url: hookUrl("fail"), name: "e2e-fail" });
  await saveHooks(page);

  await openChat(page);
  await sendMessage(page, "hook 會回 500，這句照樣要有回覆");
  await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
  expect(await receivedFor("UserPromptSubmit")).toHaveLength(1);

  await openHooksTab(page);
  const row = page.getByRole("listitem").filter({ hasText: "e2e-fail" }).first();
  await expect(row).toContainText("hook 失敗（不擋）");
  await shot(page, info, "runs", row);
});

test("UserPromptSubmit 擋下時模型收不到，畫面看得到原因", async ({ page }, info) => {
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "UserPromptSubmit", { url: hookUrl("block_prompt"), name: "e2e-block" });
  await saveHooks(page);

  await openChat(page);
  const marker = "這句不該送到模型";
  await sendMessage(page, marker);
  await expect(page.getByText(BLOCK_REASON).first()).toBeVisible();
  await shot(page, info, "blocked");
  expect((await modelCalls()).filter((call) => call.lastText.includes(marker))).toHaveLength(0);
});

test("無痕對話：hook 看得到標示，執行紀錄只有時間、事件、結果", async ({ page }, info) => {
  const hookName = "e2e-incognito-hook";
  await openHooksTab(page);
  await enableHooks(page);
  await addHttpHook(page, "UserPromptSubmit", { url: hookUrl("ok"), name: hookName });
  await saveHooks(page);

  await openChat(page);
  await page.getByRole("button", { name: "無痕對話" }).click();
  await sendMessage(page, "這是無痕對話");
  await expect(page.getByText(FAKE_TEXT_REPLY).first()).toBeVisible();
  const [hook] = await receivedFor("UserPromptSubmit");
  expect(hook.payload.incognito).toBe(true);

  const incognitoRuns = (await hookRuns(adminToken)).filter((run) => run.incognito);
  expect(incognitoRuns.length).toBeGreaterThan(0);
  for (const run of incognitoRuns) {
    expect(run.hook).toBe("");
    expect(run.detail).toBeNull();
  }
  await openHooksTab(page);
  const incognitoRow = page.getByText("（無痕）").first();
  await expect(incognitoRow).toBeVisible();
  await expect(page.getByRole("listitem").filter({ hasText: hookName })).toHaveCount(0);
  await shot(page, info, "runs", incognitoRow);
  expect((await receivedHooks()).length).toBe(1);
});
