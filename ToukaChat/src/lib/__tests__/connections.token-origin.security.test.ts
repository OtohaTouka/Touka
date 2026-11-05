/**
 * 憑證只送回發它的後端：統一出口的單元測試，以及換網址用到的幾個純函式。
 * 只有 fetch 是假的；localStorage 用測試環境的記憶體版本。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  PROXY_PROBE_TIMEOUT_MS,
  activeProxyUrlOf,
  captureRequestScope,
  clearAccessToken,
  fetchChatStream,
  fetchTouka,
  getAccessToken,
  getActiveProxyUrl,
  isProxyReachable,
  keepActiveProxy,
  logoutTouka,
  normalizeProxyUrl,
  proxyDisplayUrl,
  publishConnections,
  refreshAccessToken,
  setAccessToken,
} from '../connections';
import type { Connection } from '../types';

const A = 'http://a.test:8000';
const B = 'http://b.test:9000';
const b64 = (value: object) => btoa(JSON.stringify(value)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
const jwt = (exp: number, sub = 'u1') => `${b64({ alg: 'none' })}.${b64({ type: 'access', sub, exp })}.sig`;
const CARD = jwt(4_000_000_000);
const EXPIRED = jwt(1);
const proxy = (url: string, id = 'proxy', enabled = true) =>
  ({ id, name: id, url, provider: 'proxy', protocol: 'openai', enabled, apiKey: '' } as Connection);
const custom = (id: string) =>
  ({ id, name: id, url: `http://${id}.test`, provider: 'custom', protocol: 'openai', enabled: true, apiKey: '' } as Connection);
const useLocal = (...conns: Connection[]) => window.localStorage.setItem('touka_connections', JSON.stringify(conns));
const json = (body: unknown, init: ResponseInit = {}) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' }, ...init });

const fetchMock = vi.fn<typeof fetch>();
const sentAuth = (index = -1) => new Headers(fetchMock.mock.calls.at(index)?.[1]?.headers).get('Authorization');
const sentUrls = () => fetchMock.mock.calls.map(([url]) => String(url));

beforeEach(() => {
  clearAccessToken();
  window.localStorage.clear();
  fetchMock.mockReset();
  fetchMock.mockImplementation(async () => json({ ok: true }));
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
  clearAccessToken();
});

describe('fetchTouka 只把卡送回發它的後端', () => {
  it('對發卡後端帶卡，對別的後端不帶', async () => {
    useLocal(proxy(A));
    setAccessToken(CARD);

    await fetchTouka(A, '/v1/ui/sessions');
    expect(sentAuth()).toBe(`Bearer ${CARD}`);
    await fetchTouka(B, '/v1/ui/sessions');
    expect(sentAuth()).toBeNull();
    expect(getAccessToken()).toBe(CARD);
  });

  it('卡快過期時，對別的後端不會先去續期', async () => {
    useLocal(proxy(A));
    setAccessToken(EXPIRED);

    await fetchTouka(B, '/v1/ui/sessions');

    expect(sentUrls()).toEqual([`${B}/v1/ui/sessions`]);
    expect(sentAuth()).toBeNull();
  });

  it('呼叫端自帶 Authorization 不受影響', async () => {
    useLocal(proxy(A));
    setAccessToken(CARD);

    await fetchTouka(B, '/v1/ui/sessions', { headers: { Authorization: 'Bearer caller' } });

    expect(sentAuth()).toBe('Bearer caller');
  });

  it('別的後端回 401：不續期、不重送', async () => {
    useLocal(proxy(A));
    setAccessToken(CARD);
    fetchMock.mockImplementation(async () => json({}, { status: 401 }));

    await fetchTouka(B, '/v1/ui/sessions');

    expect(sentUrls()).toEqual([`${B}/v1/ui/sessions`]);
    expect(getAccessToken()).toBe(CARD);
  });

  it.each([
    ['尾斜線', `${A}/`],
    ['/v1', `${A}/v1`],
    ['/v1/', `${A}/v1/`],
    ['兩個斜線', `${A}//`],
    ['兩層 /v1', `${A}/v1/v1`],
  ])('網址寫成 %s 時，發卡與送卡比對得起來', async (_name, url) => {
    useLocal(proxy(url));
    setAccessToken(CARD);

    await fetchTouka(getActiveProxyUrl(), '/v1/ui/sessions');

    expect(sentAuth()).toBe(`Bearer ${CARD}`);
    expect(captureRequestScope()).not.toBeNull();
  });

  it('領卡綁發它的網址', async () => {
    useLocal(proxy(A));
    fetchMock.mockImplementationOnce(async () => json({}, { headers: { 'X-New-Auth-Token': CARD } }));

    await fetchTouka(B, '/v1/ui/sessions');
    await fetchTouka(B, '/v1/models');
    expect(sentAuth()).toBe(`Bearer ${CARD}`);
    await fetchTouka(A, '/v1/ui/connections');
    expect(sentAuth()).toBeNull();
  });

  it('refreshAccessToken 把新卡綁到回應它的網址', async () => {
    useLocal(proxy(A));
    fetchMock.mockImplementationOnce(async () => json({ access_token: CARD }));

    await refreshAccessToken(B);
    await fetchTouka(B, '/v1/ui/sessions');
    expect(sentAuth()).toBe(`Bearer ${CARD}`);
    await fetchTouka(A, '/v1/models');
    expect(sentAuth()).toBeNull();
  });

  it('同主體續期後卡仍綁原本的網址', async () => {
    useLocal(proxy(A));
    setAccessToken(EXPIRED);
    const fresh = jwt(4_000_000_000);
    fetchMock.mockImplementationOnce(async () => json({ access_token: fresh }));

    await fetchTouka(A, '/v1/ui/sessions');

    expect(sentAuth()).toBe(`Bearer ${fresh}`);
    await fetchTouka(B, '/v1/models');
    expect(sentAuth()).toBeNull();
  });

  it('網址已移動時，captureRequestScope 不再算有效', () => {
    useLocal(proxy(A));
    setAccessToken(CARD);
    expect(captureRequestScope()?.proxyOrigin).toBe(A);

    useLocal(proxy(B));

    expect(captureRequestScope()).toBeNull();
  });

  it('空字串（前端來源）也是合法的發卡後端', async () => {
    setAccessToken(CARD);

    await fetchTouka('', '/v1/ui/sessions');
    expect(sentAuth()).toBe(`Bearer ${CARD}`);
    await fetchTouka(A, '/v1/ui/sessions');
    expect(sentAuth()).toBeNull();
  });
});

describe('fetchChatStream 只把卡送回發它的後端', () => {
  const stream = (provider: 'proxy' | 'openai', baseUrl: string, apiKey?: string) =>
    fetchChatStream([{ role: 'user', content: 'hi' }], { provider, baseUrl, model: 'm', apiKey },
      () => {}, () => {}, () => {});
  const done = () => new Response('data: [DONE]\n\n', { status: 200 });

  it('對發卡後端帶卡，對別的後端不帶、也不先續期', async () => {
    useLocal(proxy(A));
    setAccessToken(CARD);
    fetchMock.mockImplementation(async () => done());

    await stream('proxy', A);
    expect(sentAuth()).toBe(`Bearer ${CARD}`);

    clearAccessToken();
    setAccessToken(EXPIRED);
    fetchMock.mockClear();
    await stream('proxy', B);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0]).startsWith(B)).toBe(true);
    expect(sentAuth()).toBeNull();
  });

  it('別的後端回 401：不續期、不重送', async () => {
    useLocal(proxy(A));
    setAccessToken(CARD);
    fetchMock.mockImplementation(async () => json({}, { status: 401 }));

    await stream('proxy', B);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('其他供應商只帶自己的金鑰，不會換成 Touka 的卡', async () => {
    useLocal(proxy(A));
    setAccessToken(CARD);
    fetchMock.mockImplementation(async () => done());

    await stream('openai', B, 'sk-provider');

    expect(sentAuth()).toBe('Bearer sk-provider');
  });

  it('領卡綁發它的網址', async () => {
    useLocal(proxy(A));
    fetchMock.mockImplementationOnce(async () =>
      new Response('data: [DONE]\n\n', { status: 200, headers: { 'X-New-Auth-Token': CARD } }));

    await stream('proxy', B);
    await fetchTouka(A, '/v1/ui/sessions');
    expect(sentAuth()).toBeNull();
    await fetchTouka(B, '/v1/ui/sessions');
    expect(sentAuth()).toBe(`Bearer ${CARD}`);
  });
});

describe('logoutTouka', () => {
  it('可以指定向哪個後端登出，並帶上取消信號', async () => {
    useLocal(proxy(A));
    setAccessToken(CARD);
    const { signal } = new AbortController();

    await logoutTouka(A, signal);

    expect(sentUrls()).toEqual([`${A}/v1/auth/logout`]);
    expect(sentAuth()).toBe(`Bearer ${CARD}`);
    expect(fetchMock.mock.calls[0][1]?.signal).toBe(signal);
    expect(getAccessToken()).toBeNull();
  });

  it('不指定時向目前的後端登出', async () => {
    useLocal(proxy(A));
    setAccessToken(CARD);

    await logoutTouka();

    expect(sentUrls()).toEqual([`${A}/v1/auth/logout`]);
  });
});

describe('網址的判斷', () => {
  it.each([
    [`${A}/`, A],
    [`${A}/v1`, A],
    [`${A}/v1/`, A],
    [A, A],
    ['', ''],
  ])('normalizeProxyUrl(%s) = %s', (input, expected) => {
    expect(normalizeProxyUrl(input)).toBe(expected);
  });

  it.each<[string, Connection[], string]>([
    ['第一筆啟用的 Proxy', [proxy(`${A}/v1`), proxy(B, 'p2')], A],
    ['略過停用的', [proxy(A, 'p1', false), proxy(B, 'p2')], B],
    ['略過其他供應商', [custom('c1'), proxy(B)], B],
    ['id 是 proxy 也算', [{ ...custom('proxy'), url: B }], B],
    ['沒有 Proxy 時用預設值（測試環境是空字串）', [custom('c1')], ''],
    ['空清單', [], ''],
  ])('activeProxyUrlOf：%s', (_name, conns, expected) => {
    expect(activeProxyUrlOf(conns)).toBe(expected);
  });

  it('getActiveProxyUrl 和 activeProxyUrlOf 判斷一致', () => {
    useLocal(proxy(`${A}/v1/`));
    expect(getActiveProxyUrl()).toBe(A);
    useLocal(custom('c1'));
    expect(getActiveProxyUrl()).toBe('');
  });

  it('proxyDisplayUrl：空字串顯示前端來源', () => {
    expect(proxyDisplayUrl(A)).toBe(A);
    expect(proxyDisplayUrl('')).toBe(window.location.origin);
  });
});

describe('keepActiveProxy：後端清單不能決定連哪裡', () => {
  it('後端算出的網址和目前相同：原樣使用', () => {
    useLocal(proxy(A, 'local'));
    const server = [proxy(`${A}/v1`, 'server'), custom('c1')];
    expect(keepActiveProxy(server)).toEqual(server);
  });

  it('不同：留本機的 Proxy，其他連線用後端的', () => {
    const local = [proxy(A, 'local'), proxy(B, 'local2', false), custom('c-local')];
    useLocal(...local);
    expect(keepActiveProxy([proxy(B, 'server'), custom('c-server')]).map(conn => conn.id))
      .toEqual(['local', 'local2', 'c-server']);
  });

  it('本機沒有 Proxy、後端有：不採用後端的 Proxy', () => {
    useLocal(custom('c-local'));
    expect(keepActiveProxy([proxy(B, 'server'), custom('c-server')]).map(conn => conn.id)).toEqual(['c-server']);
  });

  it('結果算出的網址一定和目前相同', () => {
    useLocal(proxy(A));
    expect(activeProxyUrlOf(keepActiveProxy([proxy(B)]))).toBe(A);
  });
});

describe('publishConnections', () => {
  it('先寫入再發事件，事件當下讀到的已是新網址', () => {
    useLocal(proxy(A));
    const seen: string[] = [];
    const listener = () => seen.push(getActiveProxyUrl());
    window.addEventListener('touka_connections_updated', listener);

    publishConnections([proxy(B)]);

    window.removeEventListener('touka_connections_updated', listener);
    expect(seen).toEqual([B]);
  });
});

describe('isProxyReachable', () => {
  const HEALTH = { status: 'healthy', timestamp: 't', permission_config_degraded: false, permission_config_degraded_reason: '' };

  it('健康檢查正常才算，而且不帶任何憑證', async () => {
    useLocal(proxy(A));
    setAccessToken(CARD);
    fetchMock.mockImplementation(async () => json(HEALTH));
    const timeout = vi.spyOn(AbortSignal, 'timeout');

    await expect(isProxyReachable(B)).resolves.toBe(true);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${B}/v1/health`);
    expect(init).toMatchObject({ credentials: 'omit', redirect: 'error', cache: 'no-store', referrerPolicy: 'no-referrer' });
    expect(init?.signal).toBeInstanceOf(AbortSignal);
    expect(new Headers(init?.headers).has('Authorization')).toBe(false);
    expect(timeout).toHaveBeenCalledWith(PROXY_PROBE_TIMEOUT_MS);
    timeout.mockRestore();
  });

  it.each<[string, () => Promise<Response>]>([
    ['連不上', async () => { throw new TypeError('Failed to fetch'); }],
    ['回 HTML', async () => new Response('<html></html>', { status: 200 })],
    ['回別的 JSON', async () => json({ hello: 'world' })],
    ['回 500', async () => json(HEALTH, { status: 500 })],
    ['被轉址（redirect:error 會變成例外）', async () => { throw new TypeError('redirected'); }],
  ])('%s：回 false', async (_name, answer) => {
    fetchMock.mockImplementation(answer);
    await expect(isProxyReachable(B)).resolves.toBe(false);
  });
});
