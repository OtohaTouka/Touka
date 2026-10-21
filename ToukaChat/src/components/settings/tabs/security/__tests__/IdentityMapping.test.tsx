import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { IdentityMapping } from '../IdentityMapping';
import type { IdentityMap, PermissionConfig, AgentIdentity } from '@/lib/types';

// i18n：回傳 key 以便穩定斷言（比照 AutonomyBudget.test）。
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

// Radix Select 在 jsdom 難以驅動；替換為原生 <select>（比照 SecurityTab.test 的 slider 手法）。
vi.mock('@/components/ui/select', async () => {
  const { createElement } = await import('react');
  interface Opt { props: { value: string; children: string } }
  const Select = ({ value, onValueChange, children }: {
    value: string; onValueChange?: (v: string) => void; children: React.ReactNode;
  }) => {
    const opts: Opt[] = [];
    const walk = (node: unknown) => {
      if (Array.isArray(node)) return node.forEach(walk);
      const el = node as { props?: { children?: unknown; value?: string } } | null;
      if (!el || typeof el !== 'object' || !el.props) return;
      if (typeof el.props.value === 'string') opts.push(el as Opt);
      else walk(el.props.children);
    };
    walk(children);
    return createElement(
      'select',
      { value, onChange: (e: React.ChangeEvent<HTMLSelectElement>) => onValueChange?.(e.target.value) },
      opts.map(o => createElement('option', { key: o.props.value, value: o.props.value }, o.props.children)),
    );
  };
  const Pass = ({ children }: { children?: React.ReactNode }) => children as React.ReactElement;
  const Item = ({ value, children }: { value: string; children: string }) =>
    createElement('span', { 'data-value': value }, children);
  return { Select, SelectContent: Pass, SelectItem: Item, SelectTrigger: Pass, SelectValue: () => null };
});

// 含不可指派的 Subagent／System：下拉選單必須把它們濾掉（ADR-0245，選了存檔會 422）。
const PERMISSIONS = {
  roles: { Owner: {}, Subagent: {}, Base: {}, Debugger: {}, System: {} },
} as unknown as PermissionConfig;

// 靈魂 Sumi：runtime 有效角色 Owner、能力含 can_supervise_dispatch＋heartbeat_awakening，
// 且**無 override**（tag_rules 繼承而來）。這正是「新靈魂顯示 Base、點一下降級」的場景。
const SUMI: AgentIdentity = {
  id: 'Sumi', name: 'Sumi Soul', type: 'soul', is_group: false,
  effective_role: 'Owner',
  effective_capabilities: ['heartbeat_awakening', 'can_supervise_dispatch'],
  has_override: false,
};

function renderMapping(overrides: IdentityMap['agent_overrides'] = {}, agents = [SUMI]) {
  const setIdentityMap = vi.fn();
  const identityMap: IdentityMap = {
    default_role: 'Base', tag_rules: {}, agent_overrides: overrides,
  } as IdentityMap;
  render(
    <IdentityMapping
      identityMap={identityMap}
      setIdentityMap={setIdentityMap}
      allAgents={agents}
      permissions={PERMISSIONS}
      isSavingIdentity={false}
      saveIdentity={vi.fn().mockResolvedValue(true)}
    />,
  );
  return { setIdentityMap, identityMap };
}

describe('IdentityMapping — 有效角色顯示（不誤導）', () => {
  it('無 override 的靈魂顯示有效角色 Owner，而非 default_role Base', () => {
    renderMapping();
    const row = screen.getByText('Sumi Soul').closest('tr')!;
    expect((within(row).getByRole('combobox') as HTMLSelectElement).value).toBe('Owner');
    // 標示「繼承自類型規則」而非顯示成使用者設定的空 override
    expect(within(row).getByText('settings.identity_mapping.inherited_from_type')).toBeDefined();
  });

  it('無 override 時不顯示移除鈕（沒有東西可移除）', () => {
    renderMapping();
    const row = screen.getByText('Sumi Soul').closest('tr')!;
    expect(within(row).queryByLabelText('settings.identity_mapping.remove_override_aria')).toBeNull();
  });

  it('有 override 顯示「已覆寫」標示與移除鈕', () => {
    renderMapping({ Sumi: { role: 'Owner', capabilities: ['heartbeat_awakening'] } });
    const row = screen.getByText('Sumi Soul').closest('tr')!;
    expect(within(row).getByText('settings.identity_mapping.override_active')).toBeDefined();
    expect(within(row).getByLabelText('settings.identity_mapping.remove_override_aria')).toBeDefined();
  });
});

describe('IdentityMapping — 角色下拉選單只列可指派角色', () => {
  it('Subagent／System 不出現在選項中', () => {
    renderMapping();
    const row = screen.getByText('Sumi Soul').closest('tr')!;
    const options = Array.from(within(row).getByRole('combobox').querySelectorAll('option'));
    expect(options.map(o => o.getAttribute('value'))).toEqual(['Owner', 'Base', 'Debugger']);
  });
});

describe('IdentityMapping — toggle 以有效值為底（不靜默降級）', () => {
  it('關鍵回歸：對無 override 的 Owner 靈魂 toggle 一個能力，其餘有效能力必須保留', () => {
    const { setIdentityMap } = renderMapping();
    const row = screen.getByText('Sumi Soul').closest('tr')!;
    // 關掉 heartbeat_awakening（現為 on）。t mock 不插值故 aria-label 全同 key，
    // 改以 badge 可見文字定位（cap.replace("_"," ") 只替首個底線）。
    fireEvent.click(within(row).getByText('heartbeat awakening'));

    const written = setIdentityMap.mock.calls[0][0].agent_overrides.Sumi;
    // 角色不得掉成 Base（過去 fallback {role: default_role} 的降級）
    expect(written.role).toBe('Owner');
    // can_supervise_dispatch 必須保留（Sumi 實際降級事故的核心——過去被清成 []）
    expect(written.capabilities).toContain('can_supervise_dispatch');
    expect(written.capabilities).not.toContain('heartbeat_awakening');
  });

  it('改角色也以有效值為底：能力清單不被清空', () => {
    const { setIdentityMap } = renderMapping();
    const row = screen.getByText('Sumi Soul').closest('tr')!;
    fireEvent.change(within(row).getByRole('combobox'), { target: { value: 'Debugger' } });

    const written = setIdentityMap.mock.calls[0][0].agent_overrides.Sumi;
    expect(written.role).toBe('Debugger');
    expect(written.capabilities).toEqual(
      expect.arrayContaining(['heartbeat_awakening', 'can_supervise_dispatch']),
    );
  });

  it('有效能力（can_supervise_dispatch）在能力清單可見，不被單一硬編碼能力遮蔽', () => {
    renderMapping();
    const row = screen.getByText('Sumi Soul').closest('tr')!;
    expect(within(row).getByText('can supervise_dispatch')).toBeDefined();
  });
});
