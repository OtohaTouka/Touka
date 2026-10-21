import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const putDelegation = vi.fn();
const refreshDelegations = vi.fn();

vi.mock('@/hooks/settings/useMcpWorkloadOAuth', () => ({
  useMcpWorkloadOAuth: () => ({
    delegations: [],
    busy: false,
    error: null,
    refreshDelegations,
    putDelegation,
    deleteDelegation: vi.fn(),
  }),
}));
vi.mock('@/lib/time_utils', () => ({
  getSystemNow: () => new Date('2026-08-02T00:00:00Z'),
}));

import { McpOwnerDelegationPanel } from '../McpOwnerDelegationPanel';

describe('McpOwnerDelegationPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    putDelegation.mockResolvedValue(true);
    refreshDelegations.mockResolvedValue(true);
  });

  it('只送可由 owner 指定的最小 payload，source grant 完全由後端推導', async () => {
    render(<McpOwnerDelegationPanel agentId="Alice" />);
    fireEvent.change(screen.getByLabelText('Delegation ID'), {
      target: { value: 'calendar-read' },
    });
    fireEvent.change(screen.getByLabelText('Installation ID'), {
      target: { value: 'calendar' },
    });
    fireEvent.change(screen.getByLabelText('Installation generation'), {
      target: { value: '4' },
    });
    fireEvent.change(screen.getByLabelText('Tool name'), {
      target: { value: 'calendar.read' },
    });
    fireEvent.change(screen.getByLabelText('Scopes'), {
      target: { value: 'calendar.read calendar.events.read' },
    });
    fireEvent.click(screen.getByRole('button', { name: '建立委派' }));

    await waitFor(() => expect(putDelegation).toHaveBeenCalledTimes(1));
    const [delegationId, payload] = putDelegation.mock.calls[0];
    expect(delegationId).toBe('calendar-read');
    expect(payload).toEqual({
      installation_id: 'calendar',
      installation_generation: 4,
      raw_tool_name: 'calendar.read',
      required_scopes: ['calendar.read', 'calendar.events.read'],
      expires_at: 1785715200,
    });
    expect(payload).not.toHaveProperty('source_grant_id');
    expect(payload).not.toHaveProperty('source_grant_version');
    expect(payload).not.toHaveProperty('source_profile_generation');
  });
});
