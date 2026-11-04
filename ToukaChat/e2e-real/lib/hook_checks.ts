import { expect, type Page } from "@playwright/test";
import { CANARY_API_KEY, CANARY_HOOK_HEADER, FAKE_AFTER_TOOL_REPLY, SOUL_ID } from "./constants";
import { modelCalls, receivedFor } from "./control";
import type { ReceivedHook } from "./hook_sink";

const CONVERSATION_KEYS = ["agent_id", "hook_event_name", "incognito", "session_id"];
const SHARED_KEYS = [...CONVERSATION_KEYS, "permission_mode"];
const TOOL_KEYS = ["tool_input", "tool_name"];
const TASK_KEYS = ["mission_id", "task_id", "task_subject"];

// 事件內容的鍵（白名單）：多了就可能把設定值帶出去。
const EXPECTED_KEYS: Record<string, string[]> = {
  PostToolUseFailure: [...SHARED_KEYS, ...TOOL_KEYS, "error", "tool_use_id"],
  PermissionRequest: [...SHARED_KEYS, ...TOOL_KEYS],
  PermissionDenied: [...SHARED_KEYS, ...TOOL_KEYS, "reason", "tool_use_id"],
  Notification: [...SHARED_KEYS, "message", "notification_type"],
  StopFailure: [...SHARED_KEYS, "error", "error_details"],
  SubagentStart: [...SHARED_KEYS, "agent_type"],
  SubagentStop: [...SHARED_KEYS, "agent_type", "last_assistant_message"],
  PreCompact: [...CONVERSATION_KEYS, "trigger"],
  PostCompact: [...CONVERSATION_KEYS, "compact_summary", "trigger"],
  TaskCreated: [...SHARED_KEYS, ...TASK_KEYS],
  TaskCompleted: [...SHARED_KEYS, ...TASK_KEYS],
  Elicitation: [...SHARED_KEYS, "mcp_server_name", "message", "mode", "requested_schema"],
  ConfigChange: [...CONVERSATION_KEYS, "source"],
  HeartbeatStart: [...CONVERSATION_KEYS, "home", "pulses_total"],
  HeartbeatStop: [...CONVERSATION_KEYS, "end", "pulses_done", "pulses_total"],
};

/** 內容的鍵都在白名單、也沒有設定值與金鑰。子代理的 agent_id 不是靈魂，傳 null 不比對。 */
export function expectCleanPayload(
  event: string,
  hook: ReceivedHook | undefined,
  agentId: string | null = SOUL_ID,
): asserts hook is ReceivedHook {
  expect(hook, event).toBeDefined();
  if (!hook) return;
  expect(Object.keys(hook.payload).sort(), event).toEqual([...EXPECTED_KEYS[event]].sort());
  if (agentId !== null) expect(hook.payload.agent_id, event).toBe(agentId);
  const body = JSON.stringify(hook.payload);
  expect(body, event).not.toContain(CANARY_API_KEY);
  expect(body, event).not.toContain(CANARY_HOOK_HEADER);
}

/** 背景事件不等回覆，要等它真的到了才核對。 */
export async function expectBackgroundHook(event: string, agentId: string | null = SOUL_ID): Promise<ReceivedHook> {
  await expect.poll(async () => (await receivedFor(event)).length, event).toBeGreaterThan(0);
  const [hook] = await receivedFor(event);
  expectCleanPayload(event, hook, agentId);
  return hook;
}

export function approveButton(page: Page) {
  return page.getByRole("button", { name: "批准", exact: true });
}

export function afterToolReply(page: Page) {
  return page.getByText(FAKE_AFTER_TOOL_REPLY).first();
}

export async function lastToolResultForModel(): Promise<string> {
  const toolCalls = (await modelCalls()).filter((call) => call.lastRole === "tool");
  return toolCalls.at(-1)?.lastText ?? "";
}
