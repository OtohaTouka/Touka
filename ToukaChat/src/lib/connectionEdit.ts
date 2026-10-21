/**
 * 編輯既有連線的純函式（#3）—— 金鑰保留邏輯的單一事實來源（SSOT），兩個連線 UI 共用。
 */
import type { Connection } from './types';

/** 編輯表單可改的欄位（不含 id/enabled；apiKey 僅在使用者選擇更換時才採用）。 */
export interface ConnectionDraft {
  name: string;
  url: string;
  provider: string;
  protocol: string;
  apiKey: string;
  /** P5-#4：請求逾時（毫秒）；undefined/0＝不限。 */
  requestTimeoutMs?: number;
}

/**
 * 套用編輯到既有連線。
 *
 * 安全核心：`apiKey` 只有在 `changeKey === true` 時採用 `draft.apiKey`，否則**保留
 * `existing.apiKey`**——編輯表單因此永遠不需要、也不應持有既有金鑰（不回填、不外露）。
 * `id` 與 `enabled` 不在編輯範圍內，原樣保留。空名稱回退既有名稱（不清空）。
 */
export function applyConnectionEdit(
  existing: Connection,
  draft: ConnectionDraft,
  changeKey: boolean,
): Connection {
  return {
    ...existing,
    name: draft.name || existing.name,
    url: draft.url,
    provider: draft.provider,
    protocol: draft.protocol,
    apiKey: changeKey ? draft.apiKey : existing.apiKey,
    requestTimeoutMs: draft.requestTimeoutMs,
  };
}
