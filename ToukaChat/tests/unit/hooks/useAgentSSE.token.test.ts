import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

// Mock getAccessToken
vi.mock('@/lib/connections', () => ({
  getAccessToken: vi.fn(),
}));

import { getAccessToken } from '@/lib/connections';
import { useAgentSSE } from '@/hooks/useAgentSSE';

let lastFetchUrl: string | null = null;
let lastFetchInit: RequestInit | null = null;

describe('useAgentSSE — H3 EventSource Token 攜帶驗證', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    lastFetchUrl = null;
    lastFetchInit = null;
    
    global.fetch = vi.fn((url: string, init?: RequestInit) => {
      if (url.startsWith('/stream')) {
        lastFetchUrl = url;
        lastFetchInit = init || null;
        const stream = new ReadableStream({
          start(controller) {
            controller.close();
          }
        });
        return Promise.resolve({
          ok: true,
          body: stream,
        });
      }

      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ sessionId: 'session-token-test' }),
      });
    }) as unknown as typeof fetch;
  });

  const setupHook = () => {
    return renderHook(() => useAgentSSE({ postUrl: '/api', streamUrl: '/stream' }));
  };

  it('應在 getAccessToken() 有值時將 token 帶於 fetch headers 中而非 URL 中', async () => {
    // 模擬 token 存在
    vi.mocked(getAccessToken).mockReturnValue('valid-jwt-token-xyz');

    const { result } = setupHook();
    
    await act(async () => {
      await result.current.sendMessage('hello with token');
    });

    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 0));
    });

    expect(lastFetchUrl).not.toBeNull();
    const url = new URL(lastFetchUrl!, 'http://localhost');
    expect(url.searchParams.get('sessionId')).toBe('session-token-test');
    expect(url.searchParams.has('token')).toBe(false); // URL 不可含有 token

    // 檢查 Headers 含有 Authorization
    const headers = lastFetchInit?.headers as Record<string, string>;
    expect(headers).toBeDefined();
    expect(headers['Authorization']).toBe('Bearer valid-jwt-token-xyz');
  });

  it('應在 getAccessToken() 為 null 時不攜帶 Authorization header 且不攜帶 token 參數', async () => {
    // 模擬 token 為 null
    vi.mocked(getAccessToken).mockReturnValue(null);

    const { result } = setupHook();
    
    await act(async () => {
      await result.current.sendMessage('hello without token');
    });

    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 0));
    });

    expect(lastFetchUrl).not.toBeNull();
    const url = new URL(lastFetchUrl!, 'http://localhost');
    expect(url.searchParams.get('sessionId')).toBe('session-token-test');
    expect(url.searchParams.has('token')).toBe(false);

    // 檢查 Headers 沒有 Authorization
    const headers = lastFetchInit?.headers as Record<string, string>;
    expect(headers).toBeDefined();
    expect(headers['Authorization']).toBeUndefined();
  });
});
