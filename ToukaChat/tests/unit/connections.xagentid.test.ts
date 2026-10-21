import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock useLocalStorage，使 getActiveProxyUrl() 能回傳 http://localhost:8000
vi.mock('@/hooks/useLocalStorage', () => ({
  getLocalStorageItem: vi.fn().mockReturnValue([
    {
      id: 'proxy',
      name: 'ToukaProxy',
      url: 'http://localhost:8000',
      provider: 'proxy',
      enabled: true,
    },
  ]),
}));

import { fetchToolSchemas } from '@/lib/connections';

describe('fetchToolSchemas — H2 X-Agent-ID 移除驗證', () => {
  const fetchSpy = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    fetchSpy.mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({ tools: [{ name: 'test_tool' }] }),
    } as unknown as Response);
    vi.stubGlobal('fetch', fetchSpy);
  });

  it('呼叫 fetchToolSchemas 時不應注入 X-Agent-ID header', async () => {
    const tools = await fetchToolSchemas();

    // 檢查回傳的 tools 正確
    expect(tools).toEqual([{ name: 'test_tool' }]);

    // 驗證全域 fetch 被呼叫
    expect(fetchSpy).toHaveBeenCalled();

    // 獲取 fetch 被呼叫的參數
    const calls = fetchSpy.mock.calls;
    expect(calls.length).toBe(1);

    const [url, options] = calls[0];
    expect(url).toBe('http://localhost:8000/v1/debug/tools');

    // 驗證 options 中的 headers 內不包含 X-Agent-ID (不區分大小寫)
    if (options && options.headers) {
      // 這裡可能是 Headers 物件
      let hasXAgentId = false;
      if (options.headers instanceof Headers) {
        hasXAgentId = options.headers.has('X-Agent-ID') || options.headers.has('x-agent-id');
      } else {
        const headers = options.headers as Record<string, string>;
        hasXAgentId = Object.keys(headers).some(
          (key) => key.toLowerCase() === 'x-agent-id'
        );
      }
      expect(hasXAgentId).toBe(false);
    }
  });
});
