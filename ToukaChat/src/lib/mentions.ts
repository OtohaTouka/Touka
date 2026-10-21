import { getActiveProxyUrl, fetchTouka } from "./connections";
import { logSSOTError } from "./error_handler";

/**
 * 從文字擷取 @mention 目標路徑（去重、保序）。
 * - 保留路徑內部的 `.` 與 `/`（支援 `@src/lib/utils.ts` 這類檔案路徑）與非 ASCII（中文檔名/別名）。
 * - 僅去除「尾隨」的句末標點（半形 + 全形），避免把 `@config.json。` 誤解析成不存在的 `config.json。`。
 * 抽為純函式以利單元測試（對齊 AGENTS.md 禁令 18：跨切面邏輯抽純函式）。
 */
export function extractMentionPaths(text: string): string[] {
  const mentionRegex = /@(\S+)/g;
  const paths: string[] = [];
  const seen = new Set<string>();
  for (const match of text.matchAll(mentionRegex)) {
    const path = match[1].replace(/[.,!?;:)\]}'"`。，、！？；：）】』」]+$/u, "");
    if (path && !seen.has(path)) {
      seen.add(path);
      paths.push(path);
    }
  }
  return paths;
}

export async function resolveMentions(text: string): Promise<string> {
  let resolvedContext = "";
  const proxyUrl = getActiveProxyUrl();

  for (const path of extractMentionPaths(text)) {
    // 特殊處理
    if (path === "terminal") {
      resolvedContext += `\n[Context: Recent Terminal Output]\n(Terminal context requested via @terminal)\n`;
    } else if (path === "git") {
      resolvedContext += `\n[Context: Git Status]\n(Git status requested via @git)\n`;
    } else if (path === "codebase") {
      resolvedContext += `\n[Context: Project Index]\nUse your search tools to explore the codebase.\n`;
    } else {
      // 嘗試透過 Proxy 讀取檔案 (因為 ToukaChat 是 Web 版，沒有本地權限，需透過 Proxy 的 API)
      try {
        const res = await fetchTouka(proxyUrl, `/editor/files?path=${encodeURIComponent(path)}`);
        if (res.ok) {
           const data = await res.json();
           resolvedContext += `\n--- File: ${path} ---\n${data.content}\n--- End of File ---\n`;
        }
      } catch (e) {
        logSSOTError(e, `Mentions:resolveMentions:${path}`);
      }
    }
  }

  if (resolvedContext) {
    return `[Context Mentions Resolved]\n${resolvedContext}\n[End of Context]\n\n${text}`;
  }

  return text;
}
