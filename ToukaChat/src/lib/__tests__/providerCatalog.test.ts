/**
 * providerCatalog — 供應商目錄 SSOT ＋ buildChatEndpoint 端點組裝（#2 引導＋預填）。
 *
 * buildChatEndpoint 是 chat 端點 URL 組裝的單一來源（SSOT/SOLID）：解掉「base 已含 /v1，
 * 送出時又補一次 /v1/chat/completions → 雙 /v1」的拼接 bug，並讓使用者填 base、含 /v1 的
 * base、或完整端點網址都能正確組出。PROVIDER_CATALOG 是供應商清單／預設 base_url／文件連結／
 * i18n key 的單一來源，兩個連線 UI 共用。
 */
import { describe, it, expect } from 'vitest';
import {
  PROVIDER_CATALOG,
  buildChatEndpoint,
  resolvePrefillUrl,
  type ProviderKey,
} from '../providerCatalog';
import zhTW from '@/locales/zh-TW/translation.json';
import en from '@/locales/en/translation.json';

function getKey(obj: unknown, path: string): unknown {
  return path
    .split('.')
    .reduce<unknown>(
      (acc, k) => (acc && typeof acc === 'object' ? (acc as Record<string, unknown>)[k] : undefined),
      obj,
    );
}

describe('buildChatEndpoint（/v1 穩健修正，SSOT URL 組裝）', () => {
  it('base 不含 /v1 → 補完整 /v1/chat/completions', () => {
    expect(buildChatEndpoint('openai', 'https://api.openai.com')).toBe(
      'https://api.openai.com/v1/chat/completions',
    );
  });

  it('base 已含 /v1 → 不重複（解雙 /v1）', () => {
    expect(buildChatEndpoint('openai', 'https://api.openai.com/v1')).toBe(
      'https://api.openai.com/v1/chat/completions',
    );
    expect(buildChatEndpoint('openrouter', 'https://openrouter.ai/api/v1')).toBe(
      'https://openrouter.ai/api/v1/chat/completions',
    );
  });

  it('base 已是完整端點 → 原樣（不再追加）', () => {
    expect(buildChatEndpoint('openai', 'https://api.openai.com/v1/chat/completions')).toBe(
      'https://api.openai.com/v1/chat/completions',
    );
  });

  it('去除尾斜線', () => {
    expect(buildChatEndpoint('openai', 'https://api.openai.com/')).toBe(
      'https://api.openai.com/v1/chat/completions',
    );
    expect(buildChatEndpoint('openai', 'https://api.openai.com/v1/')).toBe(
      'https://api.openai.com/v1/chat/completions',
    );
  });

  it('ollama → /api/chat（原生路徑，非 OpenAI 相容）', () => {
    expect(buildChatEndpoint('ollama', 'http://localhost:11434')).toBe(
      'http://localhost:11434/api/chat',
    );
    expect(buildChatEndpoint('ollama', 'http://localhost:11434/')).toBe(
      'http://localhost:11434/api/chat',
    );
  });

  it('proxy + anthropic → /v1/messages（含 /v1 去重）', () => {
    expect(buildChatEndpoint('proxy', 'http://localhost:8000', 'anthropic')).toBe(
      'http://localhost:8000/v1/messages',
    );
    expect(buildChatEndpoint('proxy', 'http://localhost:8000/v1', 'anthropic')).toBe(
      'http://localhost:8000/v1/messages',
    );
  });

  it('proxy + openai（預設協定）→ /v1/chat/completions', () => {
    expect(buildChatEndpoint('proxy', 'http://localhost:8000', 'openai')).toBe(
      'http://localhost:8000/v1/chat/completions',
    );
    expect(buildChatEndpoint('proxy', 'http://localhost:8000')).toBe(
      'http://localhost:8000/v1/chat/completions',
    );
  });

  it('litellm 走 OpenAI 相容路徑', () => {
    expect(buildChatEndpoint('litellm', 'http://localhost:4000')).toBe(
      'http://localhost:4000/v1/chat/completions',
    );
  });

  it('未知 provider → 退回 OpenAI 相容路徑', () => {
    expect(buildChatEndpoint('whatever', 'https://x.test')).toBe(
      'https://x.test/v1/chat/completions',
    );
  });
});

describe('PROVIDER_CATALOG（供應商目錄 SSOT 不變式）', () => {
  const ids = Object.keys(PROVIDER_CATALOG) as ProviderKey[];

  it('涵蓋五個供應商', () => {
    expect([...ids].sort()).toEqual(['litellm', 'ollama', 'openai', 'openrouter', 'proxy']);
  });

  it('每個供應商都有 icon / defaultBaseUrl / docUrl / i18nBase', () => {
    for (const id of ids) {
      const m = PROVIDER_CATALOG[id];
      expect(m.icon).toBeTruthy();
      expect(m.defaultBaseUrl).toMatch(/^https?:\/\//);
      expect(m.docUrl).toMatch(/^https:\/\//);
      expect(m.i18nBase).toBe(`connections.providers.${id}`);
    }
  });

  it('預設 base_url 經 buildChatEndpoint 不產生雙 /v1、無尾斜線', () => {
    for (const id of ids) {
      const url = buildChatEndpoint(id, PROVIDER_CATALOG[id].defaultBaseUrl);
      expect(url, `${id} 產生雙 /v1`).not.toMatch(/\/v1\/v1\//);
      expect(url, `${id} 有尾斜線`).not.toMatch(/\/$/);
    }
  });

  it('每個供應商的 label / hint key 在 zh-TW 與 en 都存在（Guard #33 對齊）', () => {
    for (const id of ids) {
      for (const leaf of ['label', 'hint']) {
        const path = `connections.providers.${id}.${leaf}`;
        expect(getKey(zhTW, path), `zh-TW 缺 ${path}`).toBeTruthy();
        expect(getKey(en, path), `en 缺 ${path}`).toBeTruthy();
      }
    }
    expect(getKey(zhTW, 'connections.providers.doc_link_label')).toBeTruthy();
    expect(getKey(en, 'connections.providers.doc_link_label')).toBeTruthy();
  });
});

describe('resolvePrefillUrl（智慧預填，不破壞使用者自訂）', () => {
  it('欄位為空 → 帶出新供應商預設值', () => {
    expect(resolvePrefillUrl('openai', '')).toBe(PROVIDER_CATALOG.openai.defaultBaseUrl);
  });

  it('仍是某供應商預設值 → 換成新供應商預設值', () => {
    expect(resolvePrefillUrl('openrouter', PROVIDER_CATALOG.openai.defaultBaseUrl)).toBe(
      PROVIDER_CATALOG.openrouter.defaultBaseUrl,
    );
  });

  it('使用者自訂值 → 保留不覆寫', () => {
    expect(resolvePrefillUrl('openai', 'https://my-private-gateway.internal')).toBe(
      'https://my-private-gateway.internal',
    );
  });

  it('未知供應商 + 空欄位 → 回空字串（無預設可帶）', () => {
    expect(resolvePrefillUrl('bogus', '')).toBe('');
  });
});
