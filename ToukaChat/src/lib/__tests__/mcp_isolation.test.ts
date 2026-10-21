/**
 * MCP 隔離設定的 422 代碼（`detail.message`）→ 畫面文字（routers/mcp.py、mcp_isolation_store、
 * mcp_hub_service）。逐筆拒絕是 `mcp_sandbox_path_refused:<第幾筆，從 0 起>:<原因>`。
 */
import { describe, expect, it } from "vitest";
import i18n from "@/lib/i18n";
import { mcpIsolationErrorMessage } from "../mcp_isolation";

const zh = i18n.getFixedT("zh-TW");
const en = i18n.getFixedT("en");

const CODES = [
  "mcp_entry_changed",
  "mcp_server_not_found",
  "mcp_origin_change_refused",
  "mcp_sandbox_paths_refused",
  "mcp_not_waiting_isolation",
  "mcp_isolation_ack_mismatch",
  "mcp_hub_install_over_self_built",
];

describe("mcpIsolationErrorMessage", () => {
  it.each(CODES)("%s has its own translation in each language", (code) => {
    const zhText = mcpIsolationErrorMessage(zh, code);
    const enText = mcpIsolationErrorMessage(en, code);
    for (const text of [zhText, enText]) {
      expect(text).toBeTruthy();
      expect(text).not.toContain("settings.");
      expect(text).not.toContain(code);
    }
    // zh-TW 缺 key 時會退回 en：兩語相同就是缺翻譯
    expect(zhText).not.toBe(enText);
  });

  it("gives every code a different message", () => {
    const texts = new Set(
      CODES.map((code) => mcpIsolationErrorMessage(zh, code)),
    );
    expect(texts.size).toBe(CODES.length);
  });

  it("names the refused folder by its 1-based row and the reason", () => {
    const detail = "mcp_sandbox_path_refused:1:protected_location";
    expect(mcpIsolationErrorMessage(zh, detail)).toMatch(/^第 2 筆：.*受保護/);
    expect(mcpIsolationErrorMessage(en, detail)).toMatch(
      /^Folder 2: .*protected/,
    );
    expect(
      mcpIsolationErrorMessage(zh, "mcp_sandbox_path_refused:0:scan_limit"),
    ).toMatch(/^第 1 筆：.*太大/);
  });

  it("does not show an unknown reason code to the user", () => {
    const text = mcpIsolationErrorMessage(
      zh,
      "mcp_sandbox_path_refused:3:new_reason",
    );
    expect(text).toMatch(/^第 4 筆：/);
    expect(text).not.toContain("new_reason");
    expect(text).not.toContain("settings.");
  });

  it.each([
    undefined,
    "",
    "VALIDATION_ERROR",
    "找不到 MCP 伺服器：x",
    "mcp_sandbox_path_refused",
    "mcp_sandbox_path_refused:x:protected_location",
  ])("leaves %j to the generic error message", (detail) => {
    expect(mcpIsolationErrorMessage(zh, detail)).toBeUndefined();
  });
});
