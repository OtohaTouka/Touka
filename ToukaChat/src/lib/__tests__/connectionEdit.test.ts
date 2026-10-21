/**
 * connectionEdit — 編輯既有連線的純函式（#3）。
 *
 * 安全核心（資安評估 high）：編輯時**永不從表單回填既有 API Key**。applyConnectionEdit 在
 * changeKey=false 時保留 existing.apiKey，前端表單因此完全不需要持有既有金鑰；只有使用者明確
 * 勾「更換金鑰」（changeKey=true）才採用 draft 的新值。
 */
import { describe, it, expect } from 'vitest';
import { applyConnectionEdit, type ConnectionDraft } from '../connectionEdit';
import type { Connection } from '../types';

const base: Connection = {
  id: 'conn-1',
  name: 'Old Name',
  url: 'https://old.example/v1',
  apiKey: 'secret-old-key',
  provider: 'openai',
  protocol: 'openai',
  enabled: true,
};

const draft: ConnectionDraft = {
  name: 'New Name',
  url: 'https://new.example/v1',
  provider: 'openrouter',
  protocol: 'anthropic',
  apiKey: 'secret-new-key',
};

describe('applyConnectionEdit（編輯連線；金鑰保留邏輯 SSOT）', () => {
  it('changeKey=false → 保留既有金鑰（永不從表單覆寫）', () => {
    expect(applyConnectionEdit(base, draft, false).apiKey).toBe('secret-old-key');
  });

  it('changeKey=true → 用 draft 的新金鑰替換', () => {
    expect(applyConnectionEdit(base, draft, true).apiKey).toBe('secret-new-key');
  });

  it('合併 name/url/provider/protocol', () => {
    const out = applyConnectionEdit(base, draft, false);
    expect(out.name).toBe('New Name');
    expect(out.url).toBe('https://new.example/v1');
    expect(out.provider).toBe('openrouter');
    expect(out.protocol).toBe('anthropic');
  });

  it('空名稱 → 回退既有名稱（不清空）', () => {
    expect(applyConnectionEdit(base, { ...draft, name: '' }, false).name).toBe('Old Name');
  });

  it('id 與 enabled 不被動到', () => {
    const out = applyConnectionEdit(base, draft, true);
    expect(out.id).toBe('conn-1');
    expect(out.enabled).toBe(true);
  });

  it('changeKey=true 但金鑰留空 → 以空字串替換（使用者明確清空，如 Ollama 免金鑰）', () => {
    expect(applyConnectionEdit(base, { ...draft, apiKey: '' }, true).apiKey).toBe('');
  });

  it('requestTimeoutMs 隨 draft 帶入（P5-#4；undefined＝清除逾時）', () => {
    expect(applyConnectionEdit(base, { ...draft, requestTimeoutMs: 30000 }, false).requestTimeoutMs).toBe(30000);
    // 既有有逾時、draft 未帶 → 視為使用者清空（回 undefined）
    expect(applyConnectionEdit({ ...base, requestTimeoutMs: 30000 }, draft, false).requestTimeoutMs).toBeUndefined();
  });
});
