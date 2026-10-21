/**
 * providerKeys — 系統級金鑰清單 SSOT ＋ isKeySet（#1）。
 */
import { describe, it, expect } from 'vitest';
import { PROVIDER_KEYS, isKeySet } from '../providerKeys';
import enJson from '@/locales/en/translation.json';
import zhJson from '@/locales/zh-TW/translation.json';

const en = enJson as { settings: { provider_keys_tab: { get_key_link?: string } } };
const zh = zhJson as { settings: { provider_keys_tab: { get_key_link?: string } } };

describe('PROVIDER_KEYS（系統金鑰清單 SSOT）', () => {
  it('涵蓋六個 provider，與精靈一致', () => {
    expect(PROVIDER_KEYS.map((p) => p.id).sort()).toEqual([
      'anthropic',
      'deepseek',
      'gemini',
      'grok',
      'openai',
      'tavily',
    ]);
  });

  it('每項都有 id / label / env，且 env 以 _API_KEY 結尾', () => {
    for (const p of PROVIDER_KEYS) {
      expect(p.id).toBeTruthy();
      expect(p.label).toBeTruthy();
      expect(p.env).toMatch(/_API_KEY$/);
    }
  });

  it('每筆都有 https 的 helpUrl（P2 取得金鑰連結）', () => {
    for (const p of PROVIDER_KEYS) {
      expect(p.helpUrl, p.id).toMatch(/^https:\/\//);
    }
  });

  it('各供應商 helpUrl 釘住預期控制台（防釣魚／防誤改）', () => {
    const byId = Object.fromEntries(PROVIDER_KEYS.map(p => [p.id, p.helpUrl]));
    expect(byId.anthropic).toBe('https://console.anthropic.com/settings/keys');
    expect(byId.openai).toBe('https://platform.openai.com/api-keys');
    expect(byId.gemini).toBe('https://aistudio.google.com/apikey');
    expect(byId.grok).toBe('https://console.x.ai/team/default/api-keys');
    expect(byId.tavily).toBe('https://app.tavily.com/home');
  });

  it('get_key_link i18n key 在 zh-TW 與 en 皆存在', () => {
    expect(en.settings.provider_keys_tab.get_key_link).toBeTruthy();
    expect(zh.settings.provider_keys_tab.get_key_link).toBeTruthy();
  });
});

describe('isKeySet（遮罩值 → 已設定判斷）', () => {
  it('空 / undefined → false（未設定）', () => {
    expect(isKeySet('')).toBe(false);
    expect(isKeySet(undefined)).toBe(false);
  });

  it('遮罩字串（非空）→ true（已設定，但前端拿不到真值）', () => {
    expect(isKeySet('••••••••')).toBe(true);
  });
});
