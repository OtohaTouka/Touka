/**
 * fetchTouka 連線逾時（P5-#4）。
 *
 * 設計：逾時為純前端 AbortController，僅在「當前連線設了 requestTimeoutMs > 0
 * 且呼叫端未自帶 signal」時套用。呼叫端自帶 signal（如 resumeChatStream 的串流）
 * 一律豁免——避免整體逾時砍掉長串流。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fetchTouka } from '../connections';

const fetchMock = vi.fn();

function setActiveConn(requestTimeoutMs?: number) {
  window.localStorage.setItem(
    'touka_connections',
    JSON.stringify([
      { id: 'proxy', name: 'P', url: 'http://x', apiKey: '', provider: 'proxy', protocol: 'openai', enabled: true, requestTimeoutMs },
    ]),
  );
}

function okResponse() {
  return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
}

describe('fetchTouka 連線逾時（P5-#4）', () => {
  beforeEach(() => {
    window.localStorage.clear();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it('連線設逾時且請求掛起 → 逾時後以 TimeoutError 中止', async () => {
    setActiveConn(20);
    fetchMock.mockImplementation((_url: string, opts: RequestInit) =>
      new Promise((_resolve, reject) => {
        opts.signal?.addEventListener('abort', () =>
          reject((opts.signal as AbortSignal & { reason?: unknown }).reason ?? new Error('aborted')),
        );
      }),
    );
    await expect(fetchTouka('http://x', '/v1/agents')).rejects.toMatchObject({ name: 'TimeoutError' });
  }, 1500);

  it('未設逾時 → 不附逾時 signal', async () => {
    setActiveConn(undefined);
    fetchMock.mockResolvedValue(okResponse());
    await fetchTouka('http://x', '/v1/agents');
    const opts = fetchMock.mock.calls[0][1] as RequestInit;
    expect(opts.signal == null).toBe(true);
  });

  it('呼叫端自帶 signal → 不套連線逾時（沿用呼叫端 signal）', async () => {
    setActiveConn(20);
    const caller = new AbortController();
    fetchMock.mockResolvedValue(okResponse());
    await fetchTouka('http://x', '/v1/agents', { signal: caller.signal });
    const opts = fetchMock.mock.calls[0][1] as RequestInit;
    expect(opts.signal).toBe(caller.signal);
  });
});
