/**
 * AgentProvider — 認證變動自癒（ADR-0153 同族；SessionListProvider 的兄弟站點）
 *
 * 根因：/login 為 public route，provider 在登入前掛載首探 GET /v1/agents 無卡吃 401
 * → agents 卡 []；登入為 router.push 客戶端導航（provider 不重掛）→ 清單要重整才出現。
 * 連鎖：agents 空 → setKnownAgentIds 空 → 聊天不帶 X-Touka-Soul-ID（身分歸屬弱化）。
 * 修法：掛載 effect 增設 touka_auth_changed 監聽，事件觸發即重探。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { AgentProvider, useAgents } from '../AgentProvider';
import { fetchTouka, setKnownAgentIds } from '@/lib/connections';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://x'),
  fetchTouka: vi.fn(),
  setKnownAgentIds: vi.fn(),
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

const fetchToukaMock = vi.mocked(fetchTouka);

const okJson = (body: unknown): Response =>
  ({ ok: true, status: 200, json: async () => body }) as unknown as Response;
const unauthorized = (): Response =>
  ({ ok: false, status: 401, json: async () => ({}) }) as unknown as Response;

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <AgentProvider>{children}</AgentProvider>
);

describe('AgentProvider — 認證變動自癒（ADR-0153 同族）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('掛載首探 401 後，touka_auth_changed 觸發重探補回 agents 並餵 setKnownAgentIds', async () => {
    fetchToukaMock.mockResolvedValueOnce(unauthorized());
    const { result } = renderHook(() => useAgents(), { wrapper });
    await waitFor(() => expect(fetchToukaMock).toHaveBeenCalledTimes(1));
    expect(result.current.agents).toHaveLength(0);

    fetchToukaMock.mockResolvedValueOnce(
      okJson([{ id: 'Sumi', name: 'Sumi', type: 'soul', is_group: false }]),
    );
    await act(async () => {
      window.dispatchEvent(new Event('touka_auth_changed'));
    });
    await waitFor(() => expect(result.current.agents.map(a => a.id)).toContain('Sumi'));
    // X-Touka-Soul-ID 解析鏈：自癒後必須把已知 agent ids 餵回 connections SSOT
    expect(vi.mocked(setKnownAgentIds)).toHaveBeenLastCalledWith(['Sumi']);
  });

  it('卸載後移除監聽（不再重探，杜絕殘留 handler 洩漏）', async () => {
    fetchToukaMock.mockResolvedValue(okJson([]));
    const { unmount } = renderHook(() => useAgents(), { wrapper });
    await waitFor(() => expect(fetchToukaMock).toHaveBeenCalledTimes(1));

    unmount();
    fetchToukaMock.mockClear();
    await act(async () => {
      window.dispatchEvent(new Event('touka_auth_changed'));
    });
    await new Promise(r => setTimeout(r, 20));
    expect(fetchToukaMock).not.toHaveBeenCalled();
  });
});
