import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as connections from '../connections';
import { getSystemEpoch } from '../time_utils';

const state = vi.hoisted(() => ({ proxy: 'http://proxy-a', stored: new Map<string, unknown>() }));
vi.mock('@/hooks/useLocalStorage', () => ({
  getLocalStorageItem: (key: string, fallback: unknown) => state.stored.get(key) ?? fallback,
  setLocalStorageItem: (key: string, value: unknown) => state.stored.set(key, value),
}));
vi.mock('../error_handler', () => ({ logSSOTError: vi.fn() }));
vi.mock('../time_utils', () => ({ getSystemEpoch: () => 1700000000000 }));

// 此測試使用真 fetchTouka 與 refresh；fetch 僅是合成 wire 邊界。
const wire = vi.fn();
const token = (sub: string, expired = false) => `e30.${btoa(JSON.stringify({
  sub, type: 'access', exp: Math.floor(getSystemEpoch() / 1000) + (expired ? -10 : 3600),
}))}.synthetic`;
const reply = (status: number, body: unknown = {}) => new Response(JSON.stringify(body), { status });

function capture() {
  // 未新增公開接縫的 baseline 仍送出相同 scoped option，直接驗 wire 不可發生。
  const api = connections as typeof connections & { captureRequestScope?: () => unknown };
  return api.captureRequestScope?.() ?? { proxyOrigin: state.proxy, authenticatedPrincipalId: 'access:A', authEpoch: -1 };
}

function scoped(scope: unknown) {
  return connections.fetchTouka(state.proxy, '/v1/ui/sessions/a', {
    method: 'POST', body: JSON.stringify({ title: 'synthetic-draft' }),
    requestScope: scope,
  } as RequestInit);
}

describe('保存操作的憑證 scope 與真正 wire 邊界', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.proxy = 'http://proxy-a';
    state.stored.clear();
    state.stored.set('touka_connections', [{ id: 'a', name: 'A', provider: 'proxy', enabled: true, url: state.proxy }]);
    state.stored.set('touka_active_connection_id', 'a');
    vi.stubGlobal('fetch', wire);
    connections.clearAccessToken();
    connections.setAccessToken(token('A'));
    wire.mockResolvedValue(reply(200));
  });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); connections.clearAccessToken(); });

  it('scoped write cancellation retains the configured request timeout', async () => {
    vi.useFakeTimers();
    state.stored.set('touka_connections', [{ id: 'a', provider: 'proxy', enabled: true, url: state.proxy, requestTimeoutMs: 100 }]);
    const owner = new AbortController();
    wire.mockImplementation((_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(options.signal.reason), { once: true });
    }));
    const pending = connections.fetchTouka(state.proxy, '/v1/ui/sessions/a', {
      method: 'POST', signal: owner.signal, requestScope: connections.captureRequestScope(),
    }).catch(error => error);
    await vi.advanceTimersByTimeAsync(101);
    const timedOut = wire.mock.calls[0][1].signal.aborted;
    owner.abort();
    await pending;
    expect(timedOut).toBe(true);
  });

  it('non-stream status/auth cancellation retains the configured timeout without leaking its option', async () => {
    vi.useFakeTimers();
    state.stored.set('touka_connections', [{ id: 'a', provider: 'proxy', enabled: true, url: state.proxy, requestTimeoutMs: 100 }]);
    const owner = new AbortController();
    wire.mockImplementation((_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(options.signal.reason), { once: true });
    }));
    const pending = connections.fetchTouka(state.proxy, '/v1/auth/login', {
      method: 'POST', signal: owner.signal, nonStreaming: true,
    }).catch(error => error);
    await vi.advanceTimersByTimeAsync(101);
    const timedOut = wire.mock.calls[0][1].signal.aborted;
    owner.abort(); await pending;
    expect(timedOut).toBe(true);
    expect(wire.mock.calls[0][1]).not.toHaveProperty('nonStreaming');
  });

  it('新 token 已裝入而 React 尚未 render，不得把 A 內容搭 B 憑證送出', async () => {
    const scope = capture();
    connections.setAccessToken(token('B'));
    await expect(scoped(scope)).rejects.toMatchObject({ name: 'AbortError' });
    expect(wire).not.toHaveBeenCalled();
  });

  it('同 principal 的明確新登入也作廢舊操作', async () => {
    const scope = capture();
    connections.setAccessToken(token('A'));
    await expect(scoped(scope)).rejects.toMatchObject({ name: 'AbortError' });
    expect(wire).not.toHaveBeenCalled();
  });

  it('登出與 malformed 主體不回退到其他使用者', async () => {
    const scope = capture();
    connections.clearAccessToken();
    await expect(scoped(scope)).rejects.toMatchObject({ name: 'AbortError' });
    connections.setAccessToken('malformed');
    await expect(scoped(capture())).rejects.toMatchObject({ name: 'AbortError' });
    expect(wire).not.toHaveBeenCalled();
  });

  it('送出前 refresh 等待期間切換 principal，只送過 refresh、mutation 為零', async () => {
    connections.setAccessToken(token('A', true));
    const scope = capture();
    let resolve!: (res: Response) => void;
    wire.mockReturnValueOnce(new Promise<Response>(done => { resolve = done; }));
    const request = scoped(scope);
    connections.setAccessToken(token('B'));
    resolve(reply(200, { access_token: token('A') }));
    await expect(request).rejects.toMatchObject({ name: 'AbortError' });
    expect(wire).toHaveBeenCalledTimes(1);
    expect(wire.mock.calls[0][0]).toBe('http://proxy-a/v1/auth/refresh');
    expect(connections.getAccessToken()).toBe(token('B'));
  });

  it('401 refresh等待後新登入，不自動重送mutation', async () => {
    const scope = capture();
    let resolve!: (res: Response) => void;
    wire.mockResolvedValueOnce(reply(401));
    wire.mockReturnValueOnce(new Promise<Response>(done => { resolve = done; }));
    const request = scoped(scope);
    await vi.waitFor(() => expect(wire).toHaveBeenCalledTimes(2));
    connections.setAccessToken(token('B'));
    resolve(reply(200, { access_token: token('A') }));
    await expect(request).rejects.toMatchObject({ name: 'AbortError' });
    expect(wire).toHaveBeenCalledTimes(2);
  });

  it('正常同principal內部refresh保留scope且單次mutation不夾帶scope至fetch', async () => {
    connections.setAccessToken(token('A', true));
    const scope = capture();
    wire.mockResolvedValueOnce(reply(200, { access_token: token('A') }));
    wire.mockResolvedValueOnce(reply(200));
    expect((await scoped(scope)).status).toBe(200);
    expect(wire).toHaveBeenCalledTimes(2);
    const options = wire.mock.calls[1][1];
    expect(options.headers.get('Authorization')).toBe(`Bearer ${token('A')}`);
    expect(options).not.toHaveProperty('requestScope');
  });

  it('Proxy切換使舊scope無效，不改投新origin', async () => {
    const scope = capture();
    state.proxy = 'http://proxy-b';
    state.stored.set('touka_connections', [{ id: 'a', name: 'B', provider: 'proxy', enabled: true, url: state.proxy }]);
    await expect(scoped(scope)).rejects.toMatchObject({ name: 'AbortError' });
    expect(wire).not.toHaveBeenCalled();
  });

  it('舊refresh的401不能撤銷已裝入的新principal token', async () => {
    connections.setAccessToken(token('A', true));
    const scope = capture();
    let resolve!: (res: Response) => void;
    wire.mockReturnValueOnce(new Promise<Response>(done => { resolve = done; }));
    const request = scoped(scope);
    connections.setAccessToken(token('B'));
    resolve(reply(401));
    await expect(request).rejects.toMatchObject({ name: 'AbortError' });
    expect(connections.getAccessToken()).toBe(token('B'));
    expect(wire).toHaveBeenCalledTimes(1);
  });
});
