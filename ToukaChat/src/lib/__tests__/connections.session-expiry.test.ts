/**
 * 修復 2＋修復 3（ADR-0144，07-11 認證事故清單 #2/#3）。
 *
 * 修復 2 — session 死亡導回登入：
 *  - refreshAccessToken 收到 401/403（後端權威判死：撤銷/過期/重放）→ 清記憶體卡
 *    ＋通知 setSessionExpiredHandler 訂閱者（AuthContext 導回 /login）。
 *  - 網路錯誤／5xx＝暫時性故障 → 保留舊卡、不通知（不誤殺 session）。
 *  - fetchTouka/fetchChatStream 帶模組卡被判 401（例：改密碼撤銷——token 未過期
 *    但已失效，送出前續期攔不到）→ single-flight 續期一次並以新卡重送一次；
 *    續期也死＝不迴圈，原 401 照回。
 *
 * 修復 3 — 曾登入的瀏覽器永不採納 LDH 降級卡：
 *  - markHasLoggedIn() 後（登入成功時由 AuthContext 標記、登出不清除），
 *    X-New-Auth-Token 一律忽略——人類面認證失敗必須走登入頁，絕不無聲換成
 *    agent 面 DebugAgent 卡（07-11 殭屍身分事故）。
 *  - 從未登入的瀏覽器維持首裝領卡相容。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  fetchTouka, fetchChatStream, refreshAccessToken,
  setAccessToken, clearAccessToken, getAccessToken,
  setSessionExpiredHandler, hasEverLoggedIn, markHasLoggedIn,
} from '../connections';
import { getSystemEpoch } from '../time_utils';

const fetchMock = vi.fn();
const onExpired = vi.fn();

function makeJwt(expEpochSec: number): string {
  const b64url = (o: object) =>
    btoa(JSON.stringify(o)).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
  return `h.${b64url({ sub: 'u', exp: expEpochSec })}.s`;
}
function nowSec(): number {
  return Math.floor(getSystemEpoch() / 1000);
}
function okResponse(headers: Record<string, string> = {}): Response {
  return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json', ...headers } });
}
function status(code: number): Response {
  return new Response('{}', { status: code });
}
function refreshResponse(accessToken: string): Response {
  return new Response(JSON.stringify({ access_token: accessToken }), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  window.localStorage.clear();
  clearAccessToken();
  fetchMock.mockReset();
  onExpired.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  setSessionExpiredHandler(onExpired);
});
afterEach(() => {
  setSessionExpiredHandler(null);
  vi.unstubAllGlobals();
  clearAccessToken();
});

describe('refreshAccessToken 判死通知（修復 2）', () => {
  it('401 → 清卡＋通知一次', async () => {
    setAccessToken(makeJwt(nowSec() + 3600));
    fetchMock.mockResolvedValue(status(401));

    const result = await refreshAccessToken('http://x');

    expect(result).toBeNull();
    expect(getAccessToken()).toBeNull();
    expect(onExpired).toHaveBeenCalledTimes(1);
  });

  it('403 → 清卡＋通知一次', async () => {
    setAccessToken(makeJwt(nowSec() + 3600));
    fetchMock.mockResolvedValue(status(403));

    await refreshAccessToken('http://x');

    expect(getAccessToken()).toBeNull();
    expect(onExpired).toHaveBeenCalledTimes(1);
  });

  it('5xx＝暫時性故障 → 保留舊卡、不通知', async () => {
    const tok = makeJwt(nowSec() + 3600);
    setAccessToken(tok);
    fetchMock.mockResolvedValue(status(503));

    await refreshAccessToken('http://x');

    expect(getAccessToken()).toBe(tok);
    expect(onExpired).not.toHaveBeenCalled();
  });

  it('網路錯誤 → 保留舊卡、不通知', async () => {
    const tok = makeJwt(nowSec() + 3600);
    setAccessToken(tok);
    fetchMock.mockRejectedValue(new TypeError('network down'));

    await refreshAccessToken('http://x');

    expect(getAccessToken()).toBe(tok);
    expect(onExpired).not.toHaveBeenCalled();
  });
});

describe('fetchTouka 帶卡 401 一次性續期重送（修復 2）', () => {
  it('401 → 續期成功 → 以新卡重送一次並回傳新回應', async () => {
    const fresh = makeJwt(nowSec() + 7200);
    setAccessToken(makeJwt(nowSec() + 3600)); // 未過期（送出前續期不觸發）但已被後端撤銷
    fetchMock
      .mockResolvedValueOnce(status(401))            // 原請求：後端判 401（修復 1 撤銷）
      .mockResolvedValueOnce(refreshResponse(fresh)) // /auth/refresh
      .mockResolvedValueOnce(okResponse());          // 重送

    const resp = await fetchTouka('http://x', '/v1/ui/sessions');

    expect(resp.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(String(fetchMock.mock.calls[1][0])).toContain('/v1/auth/refresh');
    const retryAuth = new Headers((fetchMock.mock.calls[2][1] as RequestInit).headers)
      .get('Authorization');
    expect(retryAuth).toBe(`Bearer ${fresh}`);
    expect(onExpired).not.toHaveBeenCalled();
  });

  it('401 → 續期也被判死 → 通知導回登入、原 401 照回、不迴圈', async () => {
    setAccessToken(makeJwt(nowSec() + 3600));
    fetchMock
      .mockResolvedValueOnce(status(401))  // 原請求
      .mockResolvedValueOnce(status(401)); // /auth/refresh 判死

    const resp = await fetchTouka('http://x', '/v1/ui/sessions');

    expect(resp.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(2); // 原 + refresh，無重送迴圈
    expect(onExpired).toHaveBeenCalledTimes(1);
    expect(getAccessToken()).toBeNull();
  });

  it('auth 端點自身的 401 不觸發續期重送（登入失敗＝正常業務回應）', async () => {
    setAccessToken(makeJwt(nowSec() + 3600));
    fetchMock.mockResolvedValue(status(401));

    const resp = await fetchTouka('http://x', '/v1/auth/logout', { method: 'POST' });

    expect(resp.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onExpired).not.toHaveBeenCalled();
  });

  it('未帶模組卡（未登入）的 401 不觸發續期', async () => {
    fetchMock.mockResolvedValue(status(401));

    await fetchTouka('http://x', '/v1/ui/sessions');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onExpired).not.toHaveBeenCalled();
  });
});

describe('領卡閘：曾登入的瀏覽器永不採納降級卡（修復 3）', () => {
  it('從未登入的瀏覽器 → 採納 X-New-Auth-Token（首裝精靈相容）', async () => {
    expect(hasEverLoggedIn()).toBe(false);
    fetchMock.mockResolvedValue(okResponse({ 'X-New-Auth-Token': 'first-run-card' }));

    await fetchTouka('http://x', '/v1/ui/sessions');

    expect(getAccessToken()).toBe('first-run-card');
  });

  it('曾登入的瀏覽器 → 未登入狀態也不採納（殭屍身分對釘）', async () => {
    markHasLoggedIn();
    expect(hasEverLoggedIn()).toBe(true);
    fetchMock.mockResolvedValue(okResponse({ 'X-New-Auth-Token': 'zombie-debug-card' }));

    await fetchTouka('http://x', '/v1/ui/sessions');

    expect(getAccessToken()).toBeNull();
  });

  it('fetchChatStream 領卡點同閘', async () => {
    markHasLoggedIn();
    const stream = new ReadableStream({ start(c) { c.close(); } });
    fetchMock.mockResolvedValue(new Response(stream, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'X-New-Auth-Token': 'zombie-debug-card' },
    }));

    await new Promise<void>((resolve) => {
      fetchChatStream(
        [{ role: 'user', content: 'hi' }],
        { provider: 'proxy', baseUrl: 'http://x', model: 'test-model', protocol: 'openai' },
        () => {}, () => resolve(), () => resolve(),
      );
    });

    expect(getAccessToken()).toBeNull();
  });
});

describe('fetchChatStream 帶卡 401 一次性續期重送（修復 2 鏡像）', () => {
  function emptyStreamBody(): Response {
    const stream = new ReadableStream({ start(c) { c.close(); } });
    return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }

  it('串流 401 → 續期成功 → 以新卡重打串流並正常完成', async () => {
    const fresh = makeJwt(nowSec() + 7200);
    setAccessToken(makeJwt(nowSec() + 3600));
    const onError = vi.fn();
    fetchMock
      .mockResolvedValueOnce(status(401))            // 串流：後端判 401
      .mockResolvedValueOnce(refreshResponse(fresh)) // /auth/refresh
      .mockResolvedValueOnce(emptyStreamBody());     // 重打串流

    await new Promise<void>((resolve) => {
      fetchChatStream(
        [{ role: 'user', content: 'hi' }],
        { provider: 'proxy', baseUrl: 'http://x', model: 'test-model', protocol: 'openai' },
        () => {}, () => resolve(), (e) => { onError(e); resolve(); },
      );
    });

    expect(onError).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(String(fetchMock.mock.calls[1][0])).toContain('/v1/auth/refresh');
    const retryHeaders = (fetchMock.mock.calls[2][1] as RequestInit).headers as Record<string, string>;
    expect(retryHeaders['Authorization']).toBe(`Bearer ${fresh}`);
  });

  it('串流 401 → 續期也死 → onError＋通知導回登入、不迴圈', async () => {
    setAccessToken(makeJwt(nowSec() + 3600));
    const onError = vi.fn();
    fetchMock
      .mockResolvedValueOnce(status(401))  // 串流
      .mockResolvedValueOnce(status(401)); // /auth/refresh 判死

    await new Promise<void>((resolve) => {
      fetchChatStream(
        [{ role: 'user', content: 'hi' }],
        { provider: 'proxy', baseUrl: 'http://x', model: 'test-model', protocol: 'openai' },
        () => {}, () => resolve(), (e) => { onError(e); resolve(); },
      );
    });

    expect(onError).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onExpired).toHaveBeenCalledTimes(1);
  });
});
