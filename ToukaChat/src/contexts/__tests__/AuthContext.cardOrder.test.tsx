/**
 * G15（守衛涵蓋缺口待辦）— 認證卡寫入必須先於 setIsAuthenticated。
 *
 * 缺口背景（見 docs/architecture/proposals/guard-coverage-gap-backlog.md · G15）：
 *   ADR-0153 明列載重時序假設（AuthContext.tsx 註釋）：記憶體卡（_accessToken）必須由
 *   storeToken/clearAccessToken 先更新、之後才 setIsAuthenticated——因 isAuthenticated 的
 *   useEffect 會 dispatch `touka_auth_changed` 觸發下游重探（getAccessToken），事件當下卡
 *   況必須正確。若在兩者間插入 await 或重排使 setState 先於寫卡，重探讀到過期/空卡 →
 *   /v1/memory 401 噪音 / 徽章卡 standalone（ADR-0153 修的原 bug 復發）。
 *
 *   此為「語句順序」不變式，行為級無法穩定觀測（storeToken 為同步 module 寫入、恆先於
 *   post-commit effect）。故以源碼順序鎖：每個認證轉換函式內，卡寫（storeToken/
 *   clearAccessToken）必須先於首個 setIsAuthenticated。既有 AuthContext 測試全數 mock
 *   connections，對此順序零斷言。
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const SRC = readFileSync(
  path.resolve(__dirname, '../AuthContext.tsx'),
  'utf-8',
);

/** 以大括號配對抽出 `const <name> = ... => { ... }` 的函式體。 */
function funcBody(src: string, name: string): string | null {
  const decl = new RegExp(`const ${name}\\s*=`).exec(src);
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

// 認證轉換函式：登入／登出／開機還原。三者皆會 setIsAuthenticated 並觸發 auth_changed。
const TRANSITIONS = ['initAuth', 'login', 'logout'];

describe('AuthContext 卡寫先於 setIsAuthenticated（ADR-0153／G15）', () => {
  it.each(TRANSITIONS)('%s：storeToken/clearAccessToken 先於首個 setIsAuthenticated', (name) => {
    const body = funcBody(SRC, name);
    expect(body, `找不到函式 ${name}`).toBeTruthy();
    const src = body as string;

    const cardWrite = src.search(/storeToken\(|clearAccessToken\(/);
    const setState = src.indexOf('setIsAuthenticated(');

    expect(cardWrite, `${name} 缺卡寫（storeToken/clearAccessToken）`).toBeGreaterThanOrEqual(0);
    expect(setState, `${name} 缺 setIsAuthenticated`).toBeGreaterThanOrEqual(0);
    expect(
      cardWrite < setState,
      `${name}：setIsAuthenticated 先於卡寫——事件驅動重探會讀到過期/空卡（ADR-0153 時序假設破壞）`,
    ).toBe(true);
  });
});
