/**
 * ProjectProvider — 認證變動自癒（ADR-0153 同族；SessionListProvider 的兄弟站點）
 *
 * 根因：/login 掛載首探 GET /v1/ui/projects/list 無卡吃 401 → projects 卡 [] →
 * 側欄專案分組名退化「未知專案」（useSessionGroups fallback）；登入後不重抓。
 * 修法：掛載 effect 增設 touka_auth_changed 監聽，事件觸發即重探。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { ProjectProvider, useProjects } from '../ProjectProvider';
import { fetchTouka } from '@/lib/connections';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://x'),
  fetchTouka: vi.fn(),
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

const fetchToukaMock = vi.mocked(fetchTouka);

const okJson = (body: unknown): Response =>
  ({ ok: true, status: 200, json: async () => body }) as unknown as Response;
const unauthorized = (): Response =>
  ({ ok: false, status: 401, json: async () => ({}) }) as unknown as Response;

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <ProjectProvider>{children}</ProjectProvider>
);

describe('ProjectProvider — 認證變動自癒（ADR-0153 同族）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('掛載首探 401 後，touka_auth_changed 觸發重探補回專案清單', async () => {
    fetchToukaMock.mockResolvedValueOnce(unauthorized());
    const { result } = renderHook(() => useProjects(), { wrapper });
    await waitFor(() => expect(fetchToukaMock).toHaveBeenCalledTimes(1));
    expect(result.current.projects).toHaveLength(0);

    fetchToukaMock.mockResolvedValueOnce(
      okJson([{ id: 'proj-1', name: '研究專案' }]),
    );
    await act(async () => {
      window.dispatchEvent(new Event('touka_auth_changed'));
    });
    await waitFor(() =>
      expect(result.current.projects.map(p => p.id)).toContain('proj-1'),
    );
  });

  it('卸載後移除監聽（不再重探，杜絕殘留 handler 洩漏）', async () => {
    fetchToukaMock.mockResolvedValue(okJson([]));
    const { unmount } = renderHook(() => useProjects(), { wrapper });
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
