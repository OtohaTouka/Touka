/**
 * 登入後 token 續期與領卡防挾持（批1 ①②）。
 *
 * 背景（根因）：人類 access token 效期短（1h）。本機 LDH（優雅降級）對「帶過期
 * 人類 token」的請求**不回 401**，而是靜默降級成 DebugAgent 並在 `X-New-Auth-Token`
 * 發一張 7 天的 DebugAgent 卡；舊版 fetchTouka 無條件採納 → 整個 app 被挾持成 DebugAgent。
 *
 * 修法：
 *  ① 送出前檢查 `_accessToken` 的 JWT exp，過期則先 single-flight `/auth/refresh` 再送
 *     （token 永不過期抵達後端 → LDH 永不觸發；不受睡眠/凍結影響，優於定時 timer）。
 *  ② 已登入（`_accessToken` 非空）時忽略回應的 `X-New-Auth-Token`（縱深防禦，防降級卡挾持）；
 *     未登入（首裝精靈等 pre-login）仍採納，維持 LDH 首次建卡相容。
 *
 * 修復 2（ADR-0144）契約翻轉：refresh 被後端權威判死（401/403）不再「保留舊卡」
 * ——清卡＋通知 session-expired（詳見 connections.session-expiry.test.ts）；
 * 「保留舊卡」僅存於暫時性故障（網路錯誤/5xx）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  fetchTouka, fetchChatStream,
  setAccessToken, clearAccessToken, getAccessToken,
  isTokenExpired, refreshAccessToken, setSessionExpiredHandler,
} from '../connections';
import { getSystemEpoch } from '../time_utils';

const fetchMock = vi.fn();

/** 構造一個 exp 落在指定 epoch 秒的最小 JWT（僅 payload 有意義，簽章為佔位）。 */
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
function refreshResponse(accessToken: string): Response {
  return new Response(JSON.stringify({ access_token: accessToken }), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
}

describe('isTokenExpired（JWT exp 解析，fail-safe）', () => {
  it('exp 在過去 → 過期', () => {
    expect(isTokenExpired(makeJwt(nowSec() - 10))).toBe(true);
  });
  it('exp 在遠未來 → 未過期', () => {
    expect(isTokenExpired(makeJwt(nowSec() + 3600))).toBe(false);
  });
  it('exp 在 skew 窗內（20s 後、skew 30s）→ 提前判定過期', () => {
    expect(isTokenExpired(makeJwt(nowSec() + 20), 30)).toBe(true);
  });
  it('非 JWT 亂字串 → 不判過期（fail-safe，避免解析 bug 觸發無限 refresh）', () => {
    expect(isTokenExpired('test-token-123')).toBe(false);
    expect(isTokenExpired('')).toBe(false);
  });
});

describe('fetchTouka 送出前 token 續期（①）', () => {
  beforeEach(() => {
    window.localStorage.clear();
    clearAccessToken();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    clearAccessToken();
  });

  it('token 過期 → 先打 /auth/refresh，並以新 token 送出原請求', async () => {
    const fresh = makeJwt(nowSec() + 3600);
    setAccessToken(makeJwt(nowSec() - 10));
    fetchMock
      .mockResolvedValueOnce(refreshResponse(fresh)) // /auth/refresh
      .mockResolvedValueOnce(okResponse());          // 原請求

    await fetchTouka('http://x', '/v1/ui/sessions');

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[0][0])).toContain('/v1/auth/refresh');
    const originalOpts = fetchMock.mock.calls[1][1] as RequestInit;
    const auth = new Headers(originalOpts.headers).get('Authorization');
    expect(auth).toBe(`Bearer ${fresh}`);
    expect(getAccessToken()).toBe(fresh);
  });

  it('token 有效 → 不打 refresh（僅原請求一次）', async () => {
    setAccessToken(makeJwt(nowSec() + 3600));
    fetchMock.mockResolvedValue(okResponse());

    await fetchTouka('http://x', '/v1/ui/sessions');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).not.toContain('/auth/refresh');
  });

  it('未登入（無 token）→ 不打 refresh', async () => {
    fetchMock.mockResolvedValue(okResponse());
    await fetchTouka('http://x', '/v1/ui/sessions');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).not.toContain('/auth/refresh');
  });

  it('並發多個過期請求 → refresh 只呼叫一次（single-flight）', async () => {
    const fresh = makeJwt(nowSec() + 3600);
    setAccessToken(makeJwt(nowSec() - 10));
    fetchMock.mockImplementation((url: string) =>
      String(url).includes('/auth/refresh')
        ? Promise.resolve(refreshResponse(fresh))
        : Promise.resolve(okResponse()),
    );

    await Promise.all([
      fetchTouka('http://x', '/v1/ui/sessions'),
      fetchTouka('http://x', '/v1/models'),
      fetchTouka('http://x', '/v1/ui/projects/list'),
    ]);

    const refreshCalls = fetchMock.mock.calls.filter(c => String(c[0]).includes('/auth/refresh'));
    expect(refreshCalls).toHaveLength(1);
  });

  it('auth 端點自身不觸發送出前 refresh（避免遞迴）', async () => {
    setAccessToken(makeJwt(nowSec() - 10)); // 過期
    fetchMock.mockResolvedValue(refreshResponse(makeJwt(nowSec() + 3600)));

    await fetchTouka('http://x', '/v1/auth/refresh', { method: 'POST' });

    // 只有這一次 refresh 呼叫本身，不應在它之前再插一個 refresh
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain('/v1/auth/refresh');
  });

  it('refresh 被判死（401）→ 清卡＋通知 session-expired，不無限迴圈（修復 2 契約翻轉）', async () => {
    const stale = makeJwt(nowSec() - 10);
    const onExpired = vi.fn();
    setSessionExpiredHandler(onExpired);
    setAccessToken(stale);
    fetchMock.mockImplementation((url: string) =>
      String(url).includes('/auth/refresh')
        ? Promise.resolve(new Response('{}', { status: 401 }))
        : Promise.resolve(okResponse()),
    );

    await fetchTouka('http://x', '/v1/ui/sessions');

    const refreshCalls = fetchMock.mock.calls.filter(c => String(c[0]).includes('/auth/refresh'));
    expect(refreshCalls).toHaveLength(1);           // 嘗試一次、不迴圈
    expect(getAccessToken()).toBeNull();            // 判死＝清卡（舊契約「保留舊卡」翻轉）
    expect(onExpired).toHaveBeenCalledTimes(1);     // 通知 AuthContext 導回登入
    setSessionExpiredHandler(null);
  });
});

describe('X-New-Auth-Token 領卡防挾持（②）', () => {
  beforeEach(() => {
    window.localStorage.clear();
    clearAccessToken();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    clearAccessToken();
  });

  it('已登入 → 忽略回應的 X-New-Auth-Token（不被 LDH 降級卡挾持）', async () => {
    const human = makeJwt(nowSec() + 3600);
    setAccessToken(human);
    fetchMock.mockResolvedValue(okResponse({ 'X-New-Auth-Token': 'debug-agent-card' }));

    await fetchTouka('http://x', '/v1/ui/sessions');

    expect(getAccessToken()).toBe(human);
  });

  it('未登入 → 採納 X-New-Auth-Token（LDH 首次建卡相容）', async () => {
    fetchMock.mockResolvedValue(okResponse({ 'X-New-Auth-Token': 'fresh-guest-card' }));

    await fetchTouka('http://x', '/v1/ui/sessions');

    expect(getAccessToken()).toBe('fresh-guest-card');
  });
});

describe('fetchChatStream 送出前續期 + 領卡防挾持（①②鏡像）', () => {
  beforeEach(() => {
    window.localStorage.clear();
    clearAccessToken();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    clearAccessToken();
  });

  function emptyStreamBody(headers: Record<string, string> = {}): Response {
    // 立即結束的 SSE 串流（reader 一次 done）
    const stream = new ReadableStream({ start(c) { c.close(); } });
    return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream', ...headers } });
  }

  it('過期 token → 送出前先 refresh，用新 token 打串流', async () => {
    const fresh = makeJwt(nowSec() + 3600);
    setAccessToken(makeJwt(nowSec() - 10));
    fetchMock
      .mockResolvedValueOnce(refreshResponse(fresh)) // /auth/refresh
      .mockResolvedValueOnce(emptyStreamBody());     // 串流

    await new Promise<void>((resolve) => {
      fetchChatStream(
        [{ role: 'user', content: 'hi' }],
        { provider: 'proxy', baseUrl: 'http://x', model: 'gemini-2.5-flash', protocol: 'openai' },
        () => {}, () => resolve(), () => resolve(),
      );
    });

    expect(String(fetchMock.mock.calls[0][0])).toContain('/auth/refresh');
    const streamOpts = fetchMock.mock.calls[1][1] as RequestInit;
    expect((streamOpts.headers as Record<string, string>)['Authorization']).toBe(`Bearer ${fresh}`);
  });

  it('已登入 → 串流回應的 X-New-Auth-Token 不覆蓋現有 token', async () => {
    const human = makeJwt(nowSec() + 3600);
    setAccessToken(human);
    fetchMock.mockResolvedValue(emptyStreamBody({ 'X-New-Auth-Token': 'debug-agent-card' }));

    await new Promise<void>((resolve) => {
      fetchChatStream(
        [{ role: 'user', content: 'hi' }],
        { provider: 'proxy', baseUrl: 'http://x', model: 'gemini-2.5-flash', protocol: 'openai' },
        () => {}, () => resolve(), () => resolve(),
      );
    });

    expect(getAccessToken()).toBe(human);
  });
});
