import fs from "node:fs";
import path from "node:path";
import { ENV_SANDBOX_ROOT, FAKE_MODEL_ORIGIN, HOOK_SINK_ORIGIN } from "./constants";
import type { ModelCall } from "./fake_model";
import type { ReceivedHook } from "./hook_sink";

// 假模型與 hook 接收端跑在 global-setup 的行程裡，測試行程經 HTTP 問它們。

async function getJson<T>(url: string): Promise<T> {
  // eslint-disable-next-line no-restricted-globals -- 問的是測試自己的小伺服器
  return (await (await fetch(url)).json()) as T;
}

/** 這個檔名有沒有真的出現在沙盒裡（核對工具有沒有真的執行）。 */
export function sandboxHasFile(fileName: string): boolean {
  const root = process.env[ENV_SANDBOX_ROOT];
  if (!root) throw new Error(`${ENV_SANDBOX_ROOT} is not set`);
  const entries = fs.readdirSync(root, { recursive: true, encoding: "utf8" });
  return entries.some((entry) => path.basename(entry) === fileName);
}

export async function resetRecorders(): Promise<void> {
  await getJson(`${FAKE_MODEL_ORIGIN}/_reset`);
  await getJson(`${HOOK_SINK_ORIGIN}/_reset`);
}

export function modelCalls(): Promise<ModelCall[]> {
  return getJson<ModelCall[]>(`${FAKE_MODEL_ORIGIN}/_calls`);
}

export function receivedHooks(): Promise<ReceivedHook[]> {
  return getJson<ReceivedHook[]>(`${HOOK_SINK_ORIGIN}/_received`);
}

export async function receivedFor(event: string): Promise<ReceivedHook[]> {
  return (await receivedHooks()).filter((hook) => hook.payload.hook_event_name === event);
}
