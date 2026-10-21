/**
 * HAX G15（細粒度回饋）— useFeedback hook。
 * 樂觀更新後送出 POST；失敗回滾；掛載時拉回 session 回饋對映以還原按鈕狀態。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useFeedback } from '@/hooks/useFeedback';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: () => 'http://x',
  fetchTouka: vi.fn(),
}));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import { fetchTouka } from '@/lib/connections';
const mockFetch = fetchTouka as unknown as ReturnType<typeof vi.fn>;

const okEmptyMap = { ok: true, json: async () => ({ feedbacks: {} }) };

describe('useFeedback（HAX G15）', () => {
  beforeEach(() => {
    mockFetch.mockReset();
    mockFetch.mockResolvedValue(okEmptyMap);
  });

  it('submit 樂觀更新並以正確 payload POST /v1/feedback/submit', async () => {
    const { result } = renderHook(() => useFeedback('sess1'));
    await act(async () => { await result.current.submit('msg1', 'up'); });

    expect(result.current.feedbackState['msg1']?.type).toBe('up');
    const post = mockFetch.mock.calls.find((c) => c[1] === '/v1/feedback/submit');
    expect(post).toBeTruthy();
    expect(post![2].method).toBe('POST');
    expect(JSON.parse(post![2].body)).toMatchObject({
      session_id: 'sess1',
      message_id: 'msg1',
      feedback_type: 'up',
    });
  });

  it('POST 失敗時回滾樂觀狀態', async () => {
    mockFetch.mockImplementation((_url: string, path: string) =>
      path === '/v1/feedback/submit'
        ? Promise.resolve({ ok: false, status: 500 })
        : Promise.resolve(okEmptyMap),
    );
    const { result } = renderHook(() => useFeedback('sess1'));
    await act(async () => { await result.current.submit('msg1', 'up'); });
    await waitFor(() => {
      expect(result.current.feedbackState['msg1']?.type ?? null).toBeNull();
    });
  });

  it('掛載時拉回 session 回饋對映還原狀態', async () => {
    mockFetch.mockImplementation((_url: string, path: string) =>
      path.startsWith('/v1/feedback/session/')
        ? Promise.resolve({ ok: true, json: async () => ({ feedbacks: { msgX: { type: 'down', reason: '原因' } } }) })
        : Promise.resolve(okEmptyMap),
    );
    const { result } = renderHook(() => useFeedback('sess9'));
    await waitFor(() => {
      expect(result.current.feedbackState['msgX']?.type).toBe('down');
    });
  });
});
