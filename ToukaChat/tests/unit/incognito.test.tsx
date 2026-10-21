/**
 * HAX G17（無痕模式）— 旗標出口：fetchChatStream 把 incognito 帶進送後端的 body，
 * 觸發後端零殘留閘門。其餘前端閘門（syncSession 收口、UI、匯出過濾）由源碼守衛把關。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fetchChatStream } from '@/lib/connections';

function mockFetchOnce() {
  const fetchSpy = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    headers: { get: () => null },
    body: {
      getReader: () => ({
        read: async () => ({ done: true, value: undefined }),
        releaseLock() {},
      }),
    },
  });
  vi.stubGlobal('fetch', fetchSpy);
  return fetchSpy;
}

describe('無痕旗標出口（connections fetchChatStream）', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('incognito 選項會帶進 proxy body', async () => {
    const fetchSpy = mockFetchOnce();
    await fetchChatStream(
      [{ role: 'user', content: 'hi' }] as never,
      { provider: 'proxy', baseUrl: 'http://x', model: 'm', protocol: 'anthropic', incognito: true } as never,
      () => {}, () => {}, () => {},
    );
    const body = JSON.parse(fetchSpy.mock.calls[0][1].body);
    expect(body.incognito).toBe(true);
  });

  it('未設 incognito 時 body 不為 true', async () => {
    const fetchSpy = mockFetchOnce();
    await fetchChatStream(
      [{ role: 'user', content: 'hi' }] as never,
      { provider: 'proxy', baseUrl: 'http://x', model: 'm', protocol: 'anthropic' } as never,
      () => {}, () => {}, () => {},
    );
    const body = JSON.parse(fetchSpy.mock.calls[0][1].body);
    expect(body.incognito).toBeFalsy();
  });
});
