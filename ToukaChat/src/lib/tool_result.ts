// src/lib/tool_result.ts
// P3（工具可觀測性提案 Phase 3）：把工具結果字串解析成人面卡片可渲染的判別聯集，
// 消費 P1/P2 的結構化封套——結構化錯誤（error_class）、結構化成功（ok+preview）——
// 讓卡片渲染「人話」而非原始 JSON（提案：模型面 JSON 不外顯）。純函式、可單測。

export type ToolResultKind = 'denied' | 'error' | 'success' | 'raw';

export interface ParsedToolResult {
  kind: ToolResultKind;
  message?: string;
  suggestion?: string;
  preview?: string;
  errorClass?: string;
  /** 原始文字：raw 類（或缺欄位時）由卡片直接顯示。 */
  raw: string;
}

function toText(result: unknown): string {
  if (result == null) return '';
  if (typeof result === 'string') return result;
  try {
    return JSON.stringify(result);
  } catch {
    return String(result);
  }
}

/**
 * 解析開頭的第一個 JSON 物件（容忍尾端附註，如後端的時間 footer）。
 * 後端結構化 payload 保證 JSON 開頭；先試完整 parse，失敗再抓第一個平衡物件。
 */
function firstJsonObject(text: string): Record<string, unknown> | null {
  const s = text.trimStart();
  if (!s.startsWith('{')) return null;
  try {
    return JSON.parse(s) as Record<string, unknown>;
  } catch {
    let depth = 0;
    let inStr = false;
    let esc = false;
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (inStr) {
        if (esc) esc = false;
        else if (ch === '\\') esc = true;
        else if (ch === '"') inStr = false;
        continue;
      }
      if (ch === '"') inStr = true;
      else if (ch === '{') depth++;
      else if (ch === '}') {
        depth--;
        if (depth === 0) {
          try {
            return JSON.parse(s.slice(0, i + 1)) as Record<string, unknown>;
          } catch {
            return null;
          }
        }
      }
    }
    return null;
  }
}

const asStr = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined);

export function parseToolResult(result: unknown): ParsedToolResult {
  const raw = toText(result);
  const obj = firstJsonObject(raw);
  if (obj) {
    if (typeof obj.error_class === 'string') {
      const errorClass = obj.error_class;
      return {
        kind: errorClass === 'permission_denied' ? 'denied' : 'error',
        errorClass,
        message: asStr(obj.message),
        suggestion: asStr(obj.suggestion),
        raw,
      };
    }
    if (obj.ok === true) {
      return {
        kind: 'success',
        message: asStr(obj.message),
        preview: asStr(obj.preview),
        raw,
      };
    }
  }
  return { kind: 'raw', raw };
}
