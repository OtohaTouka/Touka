import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import type { AgentIdentity, IdentityMap } from '@/lib/types';

// i18n：回傳 key 以便穩定斷言（比照 IdentityMapping.test）。
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

const state: { agents: AgentIdentity[]; map: IdentityMap; posted: IdentityMap[] } = {
  agents: [],
  map: { tag_rules: {}, agent_overrides: {}, default_role: 'Base' },
  posted: [],
};

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn((_url: string, path: string, opts?: { method?: string; body?: string }) => {
    if (path === '/v1/identity/agents') {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(state.agents) });
    }
    if (opts?.method === 'POST') {
      state.posted.push(JSON.parse(opts.body as string));
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ status: 'success' }) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve(state.map) });
  }),
}));

import { HeartbeatWhitelist } from '../HeartbeatWhitelist';

// 靈魂 Sumi：無 override，角色與能力都來自類型規則（後端算出的有效值）。
const soul = (caps: string[]): AgentIdentity => ({
  id: 'Sumi', name: 'Sumi Soul', type: 'soul', is_group: false,
  effective_role: 'Owner', effective_capabilities: caps, has_override: false,
});

beforeEach(() => {
  state.map = { tag_rules: {}, agent_overrides: {}, default_role: 'Base' };
  state.posted = [];
});

describe('HeartbeatWhitelist — 以有效角色與能力為底（不把靈魂降級成 Base）', () => {
  it('授予靈魂心跳權限：覆寫保留 Owner 與原有能力', async () => {
    state.agents = [soul(['can_supervise_dispatch'])];
    render(<HeartbeatWhitelist />);
    const select = await screen.findByRole('combobox');
    await screen.findByRole('option', { name: 'Sumi Soul' });
    fireEvent.change(select, { target: { value: 'Sumi' } });
    fireEvent.click(screen.getByText('dashboard.heartbeat_whitelist.grant_button'));

    await waitFor(() => expect(state.posted).toHaveLength(1));
    expect(state.posted[0].agent_overrides.Sumi).toEqual({
      role: 'Owner',
      capabilities: ['can_supervise_dispatch', 'heartbeat_awakening'],
    });
  });

  it('目前名單依有效能力列出：靈魂經類型規則取得的心跳權限也算', async () => {
    state.agents = [soul(['heartbeat_awakening', 'can_supervise_dispatch'])];
    render(<HeartbeatWhitelist />);
    expect(await screen.findByText('Sumi')).toBeDefined();
    expect(screen.queryByRole('option', { name: 'Sumi Soul' })).toBeNull();
  });

  it('撤銷類型規則給的心跳權限：覆寫保留角色與其他能力', async () => {
    state.agents = [soul(['heartbeat_awakening', 'can_supervise_dispatch'])];
    render(<HeartbeatWhitelist />);
    fireEvent.click(await screen.findByTitle('dashboard.heartbeat_whitelist.revoke_title'));

    await waitFor(() => expect(state.posted).toHaveLength(1));
    expect(state.posted[0].agent_overrides.Sumi).toEqual({
      role: 'Owner',
      capabilities: ['can_supervise_dispatch'],
    });
  });
});
