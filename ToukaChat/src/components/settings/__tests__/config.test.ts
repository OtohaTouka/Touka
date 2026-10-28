import { describe, it, expect } from 'vitest';
import { SETTINGS_TABS, SETTINGS_GROUPS, visibleSettingsTabs, groupedVisibleSettingsTabs } from '../config';

describe('visibleSettingsTabs (C1 role-gating SSOT)', () => {
  it('non-admin sees only non-adminOnly tabs (general + theme)', () => {
    const ids = visibleSettingsTabs(false).map((t) => t.id);
    expect(ids).toEqual(['general', 'theme']);
  });

  it('admin sees every registered tab', () => {
    const ids = visibleSettingsTabs(true).map((t) => t.id);
    expect(ids).toEqual(SETTINGS_TABS.map((t) => t.id));
    expect(ids.length).toBe(SETTINGS_TABS.length);
  });

  it('every system tab is flagged adminOnly; general & theme are not', () => {
    const byId = Object.fromEntries(SETTINGS_TABS.map((t) => [t.id, t]));
    expect(byId.general.adminOnly).toBeFalsy();
    expect(byId.theme.adminOnly).toBeFalsy();
    for (const id of ['users', 'security', 'hooks', 'env', 'sns_bots', 'provider_keys', 'group', 'mcp']) {
      expect(byId[id].adminOnly).toBe(true);
    }
  });
});

describe('groupedVisibleSettingsTabs (IA 分組 SSOT)', () => {
  it('every tab has a valid registered group', () => {
    const groupIds = new Set(SETTINGS_GROUPS.map((g) => g.id));
    for (const tab of SETTINGS_TABS) expect(groupIds.has(tab.group)).toBe(true);
  });

  it('non-admin → only the personal group with general + theme', () => {
    const grouped = groupedVisibleSettingsTabs(false);
    expect(grouped.map((g) => g.group.id)).toEqual(['personal']);
    expect(grouped[0].tabs.map((t) => t.id)).toEqual(['general', 'theme']);
  });

  it('admin → all 5 groups in order, covering every tab exactly once', () => {
    const grouped = groupedVisibleSettingsTabs(true);
    expect(grouped.map((g) => g.group.id)).toEqual([
      'personal', 'models', 'capabilities', 'conversation', 'system',
    ]);
    const flat = grouped.flatMap((g) => g.tabs.map((t) => t.id));
    expect(flat.length).toBe(SETTINGS_TABS.length); // 無孤兒、無重複
    expect(new Set(flat).size).toBe(SETTINGS_TABS.length);
    expect(new Set(flat)).toEqual(new Set(SETTINGS_TABS.map((t) => t.id)));
  });

  it('empty groups are skipped (non-admin has no models/system group)', () => {
    const ids = groupedVisibleSettingsTabs(false).map((g) => g.group.id);
    expect(ids).not.toContain('models');
    expect(ids).not.toContain('system');
  });
});
