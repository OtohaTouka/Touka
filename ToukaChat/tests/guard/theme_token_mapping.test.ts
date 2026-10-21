/**
 * 主題權杖映射完整性守衛（防「popover 下拉透明」類 bug 復發）。
 *
 * 根因（2026-06-22）：Tailwind v4 下，`bg-<token>`/`text-<token>` 等顏色工具類別
 * **只有在 `@theme` 註冊了 `--color-<token>` 時才會生成**。`scripts/sync-design.ts` 的
 * 「Core Semantic Mappings」清單曾系統性遺漏 popover / secondary / destructive，導致這些
 * 工具類別完全不解析 → 浮層面板透明、語意文字色失效。既有守衛沒驗到（對比度守衛只認
 * `tokens.ts` 內的權杖、guard_theme 只擋硬編碼調色盤色）。
 *
 * 本守衛：對 `globals.css` 內每個「原始語意色變數」`--X`，若 `src/` 元件以任何 Tailwind
 * 顏色工具類別（`bg-`/`text-`/`border-`/`ring-`/…）使用了 `-X`，則 `@theme inline` 必須
 * 存在對應 `--color-X` 映射，否則紅。自動推導、無硬編碼清單，任何新「用了卻沒映射」的
 * 語意色都會被當場攔下。
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const SRC_DIR = join(__dirname, '../../src');
const GLOBALS_CSS = join(__dirname, '../../src/app/globals.css');

const css = readFileSync(GLOBALS_CSS, 'utf8');

// 已在 @theme inline 註冊的 --color-<token> 集合。
const mapped = new Set([...css.matchAll(/--color-([a-z0-9-]+)\s*:/g)].map((m) => m[1]));

// globals.css 內所有「原始語意色變數」名（排除工具命名空間/字級/圓角/圖表色等非 Tailwind 顏色類別）。
const EXCLUDE = /^(color-|radius|font|chart-|chat-)/;
const rawTokens = [
  ...new Set([...css.matchAll(/^\s*--([a-z][a-z0-9-]*)\s*:/gim)].map((m) => m[1])),
].filter((t) => !EXCLUDE.test(t));

// 一次讀入所有 src .ts/.tsx（排除測試與 node_modules）。
function collectSource(dir: string): string {
  let text = '';
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules' || entry.name === '__tests__') continue;
      text += collectSource(join(dir, entry.name));
    } else if (/\.tsx?$/.test(entry.name)) {
      text += readFileSync(join(dir, entry.name), 'utf8') + '\n';
    }
  }
  return text;
}
const srcText = collectSource(SRC_DIR);

const UTIL = 'bg|text|border|ring|ring-offset|fill|stroke|outline|decoration|divide|from|via|to|caret|shadow';

describe('主題權杖映射完整性（防 popover 透明類 bug）', () => {
  it.each(rawTokens)(
    '語意色 --%s 若被當 Tailwind 顏色類別使用，必須有 --color-* 映射',
    (token) => {
      const used = new RegExp(`\\b(?:${UTIL})-${token}\\b`).test(srcText);
      if (!used) return; // 未使用則不要求映射
      expect(
        mapped.has(token),
        `元件使用了顏色工具 .${token}（如 bg-${token}）但 globals.css @theme inline 缺 --color-${token} 映射。` +
          `Tailwind v4 不會生成該類別 → 背景/文字失效（透明）。請於 scripts/sync-design.ts 的 ` +
          `Core Semantic Mappings 補 \`--color-${token}: var(--${token});\` 後重生 globals.css。`,
      ).toBe(true);
    },
  );
});
