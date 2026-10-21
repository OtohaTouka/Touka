import { describe, it, expect } from 'vitest';
import i18n from '@/lib/i18n';
import { SETTINGS_TABS } from '../config';
import { SETTINGS_SEARCH_INDEX, searchSettings } from '../searchIndex';

const t = (k: string) => i18n.t(k);

describe('全域設定搜尋 (searchSettings)', () => {
  it('每筆索引：tabId 合法、label i18n key 可解析（非回退原 key）', () => {
    const tabIds = new Set(SETTINGS_TABS.map((tb) => tb.id));
    for (const e of SETTINGS_SEARCH_INDEX) {
      expect(tabIds.has(e.tabId)).toBe(true);
      expect(t(e.label)).not.toBe(e.label);
      expect(t(e.label).length).toBeGreaterThan(0);
    }
  });

  it('依本地化 label / 中文關鍵字命中（健康 → 模型健康測試）', () => {
    expect(searchSettings('健康', t, true).map((e) => e.id)).toContain('model_health');
  });

  it('英文關鍵字不分 UI 語言皆命中（latency / password / backup）', () => {
    expect(searchSettings('latency', t, true).map((e) => e.id)).toContain('model_health');
    expect(searchSettings('password', t, true).map((e) => e.id)).toContain('password');
    expect(searchSettings('backup', t, true).map((e) => e.id)).toContain('backup');
  });

  it('空 / 純空白查詢 → 無結果', () => {
    expect(searchSettings('', t, true)).toEqual([]);
    expect(searchSettings('   ', t, true)).toEqual([]);
  });

  it('無符合 → 空陣列', () => {
    expect(searchSettings('zzzznomatchxyz', t, true)).toEqual([]);
  });

  it('非 admin：僅見個人分頁設定；admin-only 設定查不到', () => {
    expect(searchSettings('健康', t, false)).toEqual([]); // models 為 adminOnly
    expect(searchSettings('密碼', t, false).map((e) => e.id)).toContain('password'); // general 可見
    for (const e of searchSettings('color', t, false)) expect(['general', 'theme']).toContain(e.tabId);
  });

  it('label 子字串命中排序優先於僅 keyword 命中（代理人 → agent_reset 居首）', () => {
    const ids = searchSettings('代理人', t, true).map((e) => e.id);
    expect(ids[0]).toBe('agent_reset');
    expect(ids).toContain('group_whitelist'); // 只有 keyword「多代理人」命中
  });

  it('多 token 採 AND（每個 token 都須命中）', () => {
    expect(searchSettings('系統 備份', t, true).map((e) => e.id)).toContain('backup');
    expect(searchSettings('健康 zzznope', t, true)).toEqual([]);
  });

  it('取樣參數面板已拿掉（ADR-0250）：搜溫度／Top P／Top K 找不到，兩語翻譯也刪掉', () => {
    for (const q of ['溫度', 'temperature', 'top_p', 'top_k', '取樣', 'sampling']) {
      expect(searchSettings(q, t, true)).toEqual([]);
    }
    for (const lng of ['zh-TW', 'en']) {
      expect(i18n.exists('settings.search.entries.model_params', { lng })).toBe(false);
    }
  });
});
