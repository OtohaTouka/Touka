/**
 * Providers 認證自癒 parity 守衛（ADR-0153 同族收官）。
 *
 * 病史：ADR-0153 只補了 SystemStatusContext 與 SessionListProvider 的
 * touka_auth_changed 自癒，Agent/Model/Project 三個兄弟 provider 漏網——
 * 「/login 無卡首探 401 → 登入後不重抓 → 清單要重整才出現」＋跨帳號殘留。
 * 本守衛把「掛載即探需認證端點的 provider 必有自癒」升為分類台帳：
 * 使用 fetchTouka 的 provider 必須列入 SELF_HEALING（並實際註冊監聽）或
 * EXEMPT（附理由）——新增 provider 未分類即紅，杜絕第二次漏網。
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';

const PROVIDERS_DIR = path.resolve(__dirname, '../../src/lib/providers');

// 掛載即探需認證端點 → 必須具備 touka_auth_changed 自癒監聽
const SELF_HEALING = [
  'AgentProvider.tsx',
  'ModelProvider.tsx',
  'ProjectProvider.tsx',
  'SessionListProvider.tsx',
];

// 使用 fetchTouka 但豁免自癒的 provider——豁免必附理由（value＝人讀的依據）
const EXEMPT: Record<string, string> = {
  'ChatProvider.tsx':
    'fetchTouka 僅在使用者動作 callback（set_home 等），無掛載首探＝無 401 卡空問題',
  'StreamRegistryProvider.tsx':
    '首探閘於 sessions.length>0（resumedRef 未消耗）＝下游隨 SessionListProvider 自癒重跑',
};

describe('providers 認證自癒 parity（ADR-0153 同族守衛）', () => {
  it('SELF_HEALING 成員必須註冊且清理 touka_auth_changed 監聽', () => {
    for (const f of SELF_HEALING) {
      const src = readFileSync(path.join(PROVIDERS_DIR, f), 'utf-8');
      expect(
        /addEventListener\(["']touka_auth_changed["']/.test(src),
        `${f} 缺 touka_auth_changed addEventListener（登入後清單不自癒）`,
      ).toBe(true);
      expect(
        /removeEventListener\(["']touka_auth_changed["']/.test(src),
        `${f} 缺 removeEventListener（卸載後 handler 洩漏）`,
      ).toBe(true);
    }
  });

  it('每個使用 fetchTouka 的 provider 必須被分類——新增未分類即紅', () => {
    const files = readdirSync(PROVIDERS_DIR).filter(f => f.endsWith('.tsx'));
    expect(files.length).toBeGreaterThan(0);
    for (const f of files) {
      const src = readFileSync(path.join(PROVIDERS_DIR, f), 'utf-8');
      if (!src.includes('fetchTouka')) continue;
      expect(
        SELF_HEALING.includes(f) || f in EXEMPT,
        `${f} 使用 fetchTouka 但未分類：掛載即探請補 touka_auth_changed 監聽並列入 ` +
          `SELF_HEALING；僅使用者動作觸發／下游閘者請列入 EXEMPT 並附理由`,
      ).toBe(true);
    }
  });

  it('分類互斥：同一 provider 不得同時在 SELF_HEALING 與 EXEMPT', () => {
    for (const f of SELF_HEALING) {
      expect(f in EXEMPT, `${f} 同時出現在兩份名單`).toBe(false);
    }
  });

  // ── G19：EXEMPT 前提必須在源碼成立（守衛不能只驗分類「存在」、還要驗分類「正確」）──
  // 病史：parity 守衛只驗 provider 被歸為 EXEMPT（一個字串標籤），但沒驗豁免理由在源碼
  // 為真——ChatProvider 若新增掛載首探、或 StreamRegistry 若移除 sessions.length 閘，都會
  // 靜默重演「無卡首探 401 → 清單卡空 / 跨帳號殘留」，而守衛因分類仍在 EXEMPT 保持全綠。

  it('StreamRegistryProvider：/streams/active 首探必須續閘於 sessions.length（且閘先於探）', () => {
    const src = readFileSync(path.join(PROVIDERS_DIR, 'StreamRegistryProvider.tsx'), 'utf-8');
    const gate = src.indexOf('sessions.length === 0');
    const probe = src.indexOf('/v1/ui/streams/active');
    expect(gate, 'StreamRegistry 缺 sessions.length === 0 首探閘（EXEMPT 前提破壞）').toBeGreaterThanOrEqual(0);
    expect(probe, 'StreamRegistry 缺 /streams/active 首探').toBeGreaterThanOrEqual(0);
    expect(
      gate < probe,
      'sessions.length 閘未先於 /streams/active 首探——無卡首探 401 會回歸',
    ).toBe(true);
  });

  it('ChatProvider：不得有掛載時（空依賴 useEffect）呼叫 fetchTouka 的首探', () => {
    const src = readFileSync(path.join(PROVIDERS_DIR, 'ChatProvider.tsx'), 'utf-8');
    // 掃描每個 useEffect 的 callback body 與其依賴陣列；空依賴（[]）＝掛載時執行一次。
    const offenders: number[] = [];
    let idx = 0;
    while ((idx = src.indexOf('useEffect(', idx)) !== -1) {
      const braceStart = src.indexOf('{', idx);
      if (braceStart === -1) break;
      let depth = 0;
      let end = -1;
      for (let j = braceStart; j < src.length; j++) {
        if (src[j] === '{') depth++;
        else if (src[j] === '}' && --depth === 0) { end = j; break; }
      }
      if (end === -1) break;
      const body = src.slice(braceStart, end + 1);
      const isMount = /^\s*,\s*\[\s*\]\s*\)/.test(src.slice(end + 1, end + 40)); // ", [])"
      if (isMount && body.includes('fetchTouka')) offenders.push(idx);
      idx = end + 1;
    }
    expect(
      offenders.length,
      'ChatProvider 出現掛載時（空依賴）呼叫 fetchTouka 的首探＝EXEMPT 前提破壞；' +
        '請改列 SELF_HEALING 並補 touka_auth_changed 監聽，或維持僅使用者動作 callback',
    ).toBe(0);
  });
});
