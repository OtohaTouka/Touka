/**
 * 聊天附件 SSOT 型別（取代先前散落 4 處的 inline `{type;url}`）。
 *
 * - image：`url` = data URL（vision part）。
 * - text：`name` + `text`（抽出的檔案內容，送出時由 StreamRegistryProvider 內嵌進訊息）；
 *   `url` 對 text 無意義、固定為 ""（保持必填以免讀取端到處改 optional）。
 *
 * `type` 刻意保留 `string`（非 union）以相容舊持久化對話（過去只有 'image'）。
 */
export interface ChatAttachment {
  type: string;
  url: string;
  name?: string;
  text?: string;
  size?: number;
  mime?: string;
  // file_ref（Phase 2）：上傳文件存於 agent vault 後的相對路徑（送出時注入「請 AI 用工具讀」提示）。
  relpath?: string;
}
