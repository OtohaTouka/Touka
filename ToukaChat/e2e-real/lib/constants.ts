import os from "node:os";
import path from "node:path";

// 固定埠：前端建置時要寫死後端網址；被占用就報錯，不碰別的程式（ADR-0295）。
export const LOOPBACK = "127.0.0.1";
export const FAKE_MODEL_PORT = 4901;
export const API_PORT = 4902;
export const WEB_PORT = 4903;
export const HOOK_SINK_PORT = 4904;
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

// 假模型看使用者訊息裡的標記決定怎麼回。
const MARK_CALL_WRITE = "E2E_CALL_WRITE";
export const WRITE_REQUEST_PATTERN = new RegExp(`${MARK_CALL_WRITE}:(\\S+)`);

/** 使用者訊息裡的標記：假模型看到就叫 write 工具，寫這個檔名。 */
export function writeRequest(fileName: string): string {
  return `${MARK_CALL_WRITE}:${fileName} 請寫一個檔案`;
}
export const FAKE_TEXT_REPLY = "假模型的回覆：收到。";
export const FAKE_AFTER_TOOL_REPLY = "假模型的回覆：工具結果收到。";

// global-setup 交給測試行程的環境變數（只有沙盒帳號）。
export const ENV_EMAIL = "E2E_REAL_EMAIL";
export const ENV_PASSWORD = "E2E_REAL_PASSWORD";
export const ENV_SANDBOX_ROOT = "E2E_REAL_SANDBOX_ROOT";
