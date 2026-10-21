/**
 * G24（守衛涵蓋缺口待辦）— 截圖失敗不再靜默：handleScreenshot 三分支回饋。
 *
 * 缺口背景（見 docs/architecture/proposals/guard-coverage-gap-backlog.md · G24）：
 *   ADR-0161：handleScreenshot 於 getDisplayMedia API 缺席須 toast.warning（明示不支援）、
 *   擷取錯誤須 logSSOTError＋toast.error（fail-loud），僅使用者主動取消（DOMException
 *   NotAllowedError）維持靜默。三分支若回歸「直接 return／catch 全吞」，使用者點相機毫無
 *   回饋＝體感按鈕壞了（正是本 ADR 修的 bug）。
 *
 *   此不變式**先前零測試**：handleScreenshot 在大元件 page.tsx 內、getDisplayMedia 僅出現
 *   於 production，唯一相關測試 ChatArea.test 斷言的是 vi.fn mock（不觸真實邏輯）；
 *   Guard #33 只驗 i18n key 存在同步（不擋未用鍵、不驗行為）。
 *
 * page.tsx 依賴大量 provider、單元渲染不切實際；故以源碼掃描鎖三分支存在與「取消先於
 * 報錯」的順序（任一分支回歸為靜默即紅）。
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const SRC = readFileSync(path.resolve(__dirname, '../page.tsx'), 'utf-8');

/** 以大括號配對抽出 `const handleScreenshot = async () => { ... }` 的函式體。 */
function handleScreenshotBody(src: string): string | null {
  const decl = /const handleScreenshot\s*=/.exec(src);
  if (!decl) return null;
  const braceStart = src.indexOf('{', src.indexOf('=>', decl.index));
  if (braceStart === -1) return null;
  let depth = 0;
  for (let j = braceStart; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(braceStart, j + 1);
  }
  return null;
}

describe('handleScreenshot 三分支回饋（ADR-0161／G24）', () => {
  const body = handleScreenshotBody(SRC);

  it('handleScreenshot 存在且可抽出函式體', () => {
    expect(body, '找不到 handleScreenshot').toBeTruthy();
  });

  it('API 缺席分支：getDisplayMedia 缺席時 toast.warning', () => {
    const src = body as string;
    expect(src.includes('getDisplayMedia'), '缺 getDisplayMedia API 缺席判斷').toBe(true);
    expect(src.includes('toast.warning'), 'API 缺席未 toast.warning＝靜默回歸').toBe(true);
  });

  it('使用者取消分支：NotAllowedError 維持靜默（return），且先於 fail-loud 報錯', () => {
    const src = body as string;
    const cancel = src.indexOf('NotAllowedError');
    const failLoud = src.indexOf('toast.error');
    expect(cancel, '缺 NotAllowedError 取消判斷（使用者拒絕會被誤報 error）').toBeGreaterThanOrEqual(0);
    expect(failLoud, '缺 toast.error fail-loud 分支').toBeGreaterThanOrEqual(0);
    expect(cancel < failLoud, 'NotAllowedError 靜默判斷未先於 toast.error＝取消被誤報').toBe(true);
  });

  it('fail-loud 分支：其餘擷取錯誤 logSSOTError＋toast.error', () => {
    const src = body as string;
    expect(src.includes('logSSOTError'), '擷取錯誤未 logSSOTError＝靜默吞錯').toBe(true);
    expect(src.includes('toast.error'), '擷取錯誤未 toast.error＝靜默回歸').toBe(true);
  });
});
