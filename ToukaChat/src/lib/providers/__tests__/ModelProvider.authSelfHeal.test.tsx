/**
 * ModelProvider — 認證變動自癒（ADR-0153 同族；SessionListProvider 的兄弟站點）
 *
 * 根因：/login 掛載首探 GET /v1/models 無卡吃 401 → 模型選單空；登入後 provider 不重掛
 * （router.push）、原本只監聽 touka_refresh_models（設定頁才發射）→ 要重整才出現。
 * 修法：掛載 effect 增設 touka_auth_changed 監聽，事件觸發即重探。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { ModelProvider, useModels } from '../ModelProvider';
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
  <ModelProvider>{children}</ModelProvider>
);

describe('ModelProvider — 認證變動自癒（ADR-0153 同族）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('掛載首探 401（選單空）後，touka_auth_changed 觸發重探補回模型清單', async () => {
    fetchToukaMock.mockResolvedValueOnce(unauthorized());
    const { result } = renderHook(() => useModels(), { wrapper });
    await waitFor(() => expect(fetchToukaMock).toHaveBeenCalledTimes(1));
    expect(result.current.allModels).toHaveLength(0);

    fetchToukaMock.mockResolvedValueOnce(
      okJson({ data: [{ id: 'ToukaSoulSumi', name: 'Sumi', owned_by: 'touka', enabled: true }] }),
    );
    await act(async () => {
      window.dispatchEvent(new Event('touka_auth_changed'));
    });
    await waitFor(() =>
      expect(result.current.allModels.map(m => m.id)).toContain('ToukaSoulSumi'),
    );
  });

  it('既有 touka_refresh_models 監聽不受影響（兩事件並存）', async () => {
    fetchToukaMock.mockResolvedValue(okJson({ data: [] }));
    renderHook(() => useModels(), { wrapper });
    await waitFor(() => expect(fetchToukaMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      window.dispatchEvent(new Event('touka_refresh_models'));
    });
    await waitFor(() => expect(fetchToukaMock).toHaveBeenCalledTimes(2));
  });

  it('卸載後移除監聽（不再重探，杜絕殘留 handler 洩漏）', async () => {
    fetchToukaMock.mockResolvedValue(okJson({ data: [] }));
    const { unmount } = renderHook(() => useModels(), { wrapper });
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
