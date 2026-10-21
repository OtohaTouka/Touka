/**
 * SessionListProvider — 認證變動自癒（ADR-0153 同族）
 *
 * 根因：掛載時的 GET /v1/ui/sessions 若在 auth 尚未還原／token 動盪期間送出，會吃 401
 * （res.ok=false）→ refreshSessions 靜默回 [] 不動 sessions → 側邊欄空著；且原本無
 * touka_auth_changed 重探監聽（相對 SystemStatusContext 已於 ADR-0153 補上），空列表在
 * 該頁生命週期內不自癒，直到手動重整。實機事故：admin 編輯使用者觸發 token 撤銷 →
 * 隨後 /v1/ui/sessions 吃 401 → 與 Sumi 的家對話從側欄消失（資料完好，純顯示層）。
 *
 * 修法：掛載 effect 增設 touka_auth_changed 監聽（AuthContext 於登入／開機還原成功／
 * 登出／session 判死時發射），事件觸發即重探一次 → 認證就緒後列表自動補回。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { SessionListProvider, useSessionList } from '../SessionListProvider';
import { fetchTouka } from '@/lib/connections';
import type { Session } from '@/lib/types';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://x'),
  fetchTouka: vi.fn(),
  captureRequestScope: () => ({ proxyOrigin: 'http://x', authenticatedPrincipalId: 'a', authEpoch: 1 }),
  isRequestScopeCurrent: () => true, subscribeCredentialScope: () => () => {},
}));
vi.mock('@/lib/error_handler', () => ({
  logSSOTError: vi.fn(),
  getErrorMessage: vi.fn(async () => 'x'),
  errorMsg: vi.fn(() => 'x'),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

const fetchToukaMock = vi.mocked(fetchTouka);

const okJson = (body: unknown): Response =>
  ({ ok: true, status: 200, json: async () => body }) as unknown as Response;
const unauthorized = (): Response =>
  ({ ok: false, status: 401, json: async () => ({}) }) as unknown as Response;

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SessionListProvider>{children}</SessionListProvider>
);

const sess = (id: string, updatedAt: number): Session =>
  ({ id, title: id, messages: [], updatedAt, agent_id: 'Sumi' }) as unknown as Session;

describe('SessionListProvider — 認證變動自癒（ADR-0153 同族）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('掛載首探 401（空側邊欄）後，touka_auth_changed 觸發重探把 Sumi 家對話補回', async () => {
    // 掛載：auth 未就緒／token 動盪 → 401 → sessions 卡 []
    fetchToukaMock.mockResolvedValueOnce(unauthorized());
    const { result } = renderHook(() => useSessionList(), { wrapper });
    await waitFor(() => expect(fetchToukaMock).toHaveBeenCalledTimes(1));
    expect(result.current.sessions).toHaveLength(0);

    // 認證就緒（登入／開機還原成功）→ AuthContext 發事件 → 重探（後端回 Sumi 家對話）
    fetchToukaMock.mockResolvedValueOnce(
      okJson([sess('session-1783869792884', 1783870122927)]),
    );
    await act(async () => {
      window.dispatchEvent(new Event('touka_auth_changed'));
    });
    await waitFor(() =>
      expect(result.current.sessions.map(s => s.id)).toContain('session-1783869792884'),
    );
  });

  it('卸載後移除 touka_auth_changed 監聽（不再重探，杜絕殘留 handler 洩漏）', async () => {
    fetchToukaMock.mockResolvedValue(okJson([]));
    const { unmount } = renderHook(() => useSessionList(), { wrapper });
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
