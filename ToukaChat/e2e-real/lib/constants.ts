import os from "node:os";
import path from "node:path";

// 固定埠：前端建置時要寫死後端網址；被占用就報錯，不碰別的程式（ADR-0295）。
export const LOOPBACK = "127.0.0.1";
export const FAKE_MODEL_PORT = 4901;
export const API_PORT = 4902;
export const WEB_PORT = 4903;
export const HOOK_SINK_PORT = 4904;
export const FAKE_MCP_PORT = 4905;
export const FAKE_MODEL_ORIGIN = `http://${LOOPBACK}:${FAKE_MODEL_PORT}`;
export const API_ORIGIN = `http://${LOOPBACK}:${API_PORT}`;
export const WEB_ORIGIN = `http://${LOOPBACK}:${WEB_PORT}`;
export const HOOK_SINK_ORIGIN = `http://${LOOPBACK}:${HOOK_SINK_PORT}`;

export const CHAT_ROOT = path.resolve(__dirname, "..", "..");
export const REPO_ROOT = path.resolve(CHAT_ROOT, "..");

// 輸出都在 repo 外面；CI 把這個資料夾傳成產物。
export const OUTPUT_DIR = process.env.E2E_REAL_OUTPUT_DIR || path.join(os.tmpdir(), "touka-e2e-real");
export const SHOTS_DIR = path.join(OUTPUT_DIR, "shots");
export const BACKEND_LOG = path.join(OUTPUT_DIR, "backend.log");

export const SOUL_ID = "Alice";
export const FAKE_MODEL_ID = "fake-chat";
// 不是真的金鑰：用來確認設定值不會出現在 hook 的事件內容裡。
export const CANARY_API_KEY = "e2e-canary-not-a-real-key";
export const CANARY_HOOK_HEADER = "e2e-canary-hook-header";

export const ASK_QUESTION = "實際驗證：要繼續嗎？";
export const ASK_ANSWER = "繼續";
// 和 tests/e2e/fake_mcp_server.py 同一句
export const MCP_FORM_MESSAGE = "實際驗證：請填這個欄位";
export const MCP_SERVER = "e2e";
export const MCP_TOOL = `${MCP_SERVER}__ask_form`;
export const SUBAGENT_GOAL = "實際驗證：子代理的目標";
export const TASK_SUBJECTS = { T1: "實際驗證：第一個任務", T2: "實際驗證：第二個任務" };

// 假模型看使用者訊息裡的標記決定叫哪個工具，`:` 後面是檔名。
const TOOL_REQUESTS: Record<string, { tool: string; args: (fileName: string) => object }> = {
  E2E_CALL_WRITE: { tool: "write", args: (path) => ({ scope: "personal", path, content: "x" }) },
  // 讀不存在的檔：工具開始跑之後才失敗
  E2E_CALL_READ: { tool: "read", args: (path) => ({ scope: "personal", path }) },
  // 少了必要參數 content
  E2E_CALL_BAD_WRITE: { tool: "write", args: (path) => ({ scope: "personal", path }) },
  E2E_CALL_ASK: {
    tool: "ask_user",
    args: () => ({ questions: [{ question: ASK_QUESTION, options: [ASK_ANSWER, "停下"] }] }),
  },
  E2E_CALL_SUBAGENT: { tool: "dispatch_subagent", args: () => ({ goal: SUBAGENT_GOAL }) },
  E2E_CALL_MISSION: {
    tool: "mission_tools",
    args: () => ({
      action: "initialize",
      goal: "實際驗證：任務看板",
      tasks: Object.entries(TASK_SUBJECTS).map(([task_id, description]) => ({ task_id, description })),
    }),
  },
  E2E_CALL_TASK_DOING: {
    tool: "mission_tools",
    args: (task_id) => ({ action: "update_status", task_id, status: "DOING" }),
  },
  E2E_CALL_TASK_DONE: {
    tool: "mission_tools",
    args: (task_id) => ({ action: "update_status", task_id, status: "DONE" }),
  },
  E2E_CALL_MCP: { tool: MCP_TOOL, args: () => ({}) },
};

/** 使用者訊息裡的標記對應到的工具呼叫；沒有標記回 null。 */
export function toolRequestIn(text: string): { tool: string; args: string } | null {
  const match = text.match(/(E2E_CALL_[A-Z_]+):(\S+)/);
  const request = match && TOOL_REQUESTS[match[1]];
  return request ? { tool: request.tool, args: JSON.stringify(request.args(match[2])) } : null;
}

/** 假模型看到就叫 write 工具，寫這個檔名。 */
export function writeRequest(fileName: string): string {
  return `E2E_CALL_WRITE:${fileName} 請寫一個檔案`;
}

export function readRequest(fileName: string): string {
  return `E2E_CALL_READ:${fileName} 請讀這個檔案`;
}

export function badWriteRequest(fileName: string): string {
  return `E2E_CALL_BAD_WRITE:${fileName} 請寫一個檔案`;
}

export function askRequest(): string {
  return "E2E_CALL_ASK:question 請問我問題";
}

export function subagentRequest(): string {
  return "E2E_CALL_SUBAGENT:go 請派子代理";
}

export function missionRequest(): string {
  return "E2E_CALL_MISSION:go 請建立任務";
}

export function taskRequest(status: "DOING" | "DONE", taskId: string): string {
  return `E2E_CALL_TASK_${status}:${taskId} 請更新任務`;
}

export function mcpRequest(): string {
  return "E2E_CALL_MCP:go 請用 MCP 工具";
}

// 假模型看到就不回答：回 400（內容帶金鑰，要被遮掉），或回 200 但內容不是 JSON。
export const MODEL_400_REQUEST = "E2E_MODEL_400 這則會失敗";
export const MODEL_BAD_JSON_REQUEST = "E2E_MODEL_BAD_JSON 這則會失敗";
export const MODEL_NO_REPLY_REQUEST = "E2E_MODEL_NO_REPLY 這則會失敗";
export const MODEL_EMPTY_REPLY_REQUEST = "E2E_MODEL_EMPTY_REPLY 這則會失敗";
export const FAKE_TEXT_REPLY = "假模型的回覆：收到。";
export const FAKE_AFTER_TOOL_REPLY = "假模型的回覆：工具結果收到。";

// global-setup 交給測試行程的環境變數（只有沙盒帳號）。
export const ENV_EMAIL = "E2E_REAL_EMAIL";
export const ENV_PASSWORD = "E2E_REAL_PASSWORD";
export const ENV_SANDBOX_ROOT = "E2E_REAL_SANDBOX_ROOT";
export const ENV_HEARTBEAT_TOKEN = "E2E_REAL_HEARTBEAT_TOKEN";
