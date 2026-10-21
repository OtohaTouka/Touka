import type { TFunction } from "i18next";

/**
 * MCP 隔離設定（來源、額外允許的資料夾）的 422 代碼 → 畫面文字。
 *
 * 後端沿用 `VALIDATION_ERROR`，把代碼放在 `detail.message`（routers/mcp.py、
 * mcp_isolation_store、mcp_hub_service）；逐筆拒絕的資料夾是
 * `mcp_sandbox_path_refused:<第幾筆，從 0 起>:<原因>`。
 */
/** 條目在讀取之後被改過；呼叫端要重新讀一次 `bound` 再讓人確認。 */
export const MCP_ENTRY_CHANGED = "mcp_entry_changed";
const MCP_ISOLATION_CODES: readonly string[] = [
  MCP_ENTRY_CHANGED,
  "mcp_server_not_found",
  "mcp_origin_change_refused",
  "mcp_sandbox_paths_refused",
  "mcp_not_waiting_isolation",
  "mcp_isolation_ack_mismatch",
  "mcp_hub_install_over_self_built",
];
const SANDBOX_PATH_REFUSED = "mcp_sandbox_path_refused";
// 永不授權的原因（utils/touka_dirs.py）；其他原因不把代碼原文顯示給人
const SANDBOX_PATH_REASONS: readonly string[] = [
  "protected_location",
  "scan_limit",
];
const KEY = "settings.mcp_isolation.errors";

/** 隔離設定的代碼轉成畫面文字；不是這組代碼回 undefined（交給一般錯誤訊息）。 */
export function mcpIsolationErrorMessage(
  t: TFunction,
  detail: string | undefined,
): string | undefined {
  if (!detail) return undefined;
  if (MCP_ISOLATION_CODES.includes(detail)) return t(`${KEY}.${detail}`);
  const [code, index, ...rest] = detail.split(":");
  if (code !== SANDBOX_PATH_REFUSED || !/^\d+$/.test(index ?? "")) {
    return undefined;
  }
  const reason = rest.join(":");
  const known = SANDBOX_PATH_REASONS.includes(reason);
  return t(`${KEY}.${SANDBOX_PATH_REFUSED}`, {
    n: Number(index) + 1,
    reason: t(`${KEY}.path_reasons.${known ? reason : "other"}`),
  });
}
