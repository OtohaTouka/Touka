/**
 * logoutTouka() 行為修復測試
 *
 * Bug 修復：logoutTouka() 不應再呼叫 window.location.reload()。
 * 導向 /login 的責任移交給 AuthContext.logout()，以確保
 * React state 正確清空後再觸發 AuthGuard 的路由跳轉。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

// 直接 import 真實模組（測試 module-level _accessToken 的副作用）
import {
  logoutTouka, setAccessToken, getAccessToken,
  clearAccessToken, markHasLoggedIn, hasEverLoggedIn,
} from '../connections';

describe('logoutTouka()', () => {
    beforeEach(() => {
        vi.restoreAllMocks();
        setAccessToken('test-token-123');
    });

    it('修復後不再呼叫 window.location.reload()', async () => {
        const reloadSpy = vi.fn();
        Object.defineProperty(window, 'location', {
            value: { reload: reloadSpy },
            writable: true,
            configurable: true,
        });

        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
            ok: true,
            status: 200,
            headers: new Headers(),
        }));

        await logoutTouka();

        expect(reloadSpy).not.toHaveBeenCalled();
    });

    it('清除記憶體中的 _accessToken', async () => {
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
            ok: true,
            status: 200,
            headers: new Headers(),
        }));

        await logoutTouka();

        expect(getAccessToken()).toBeNull();
    });

    it('呼叫後端 POST /v1/auth/logout', async () => {
        const fetchSpy = vi.fn().mockResolvedValue({
            ok: true,
            status: 200,
            headers: new Headers(),
        });
        vi.stubGlobal('fetch', fetchSpy);

        await logoutTouka();

        const called = fetchSpy.mock.calls.some(([url]) =>
            String(url).includes('/v1/auth/logout')
        );
        expect(called).toBe(true);
    });
});

// ── G05（守衛涵蓋缺口 G06）：touka_has_logged_in 登出「不」清除 ────────────────────
// ADR-0144 修復 3 加粗承諾：曾登入旗標於登入標記、登出不清——否則登出後瀏覽器又變回
// 「首裝相容」→ 重新採納 X-New-Auth-Token（LDH 降級卡），殭屍身分防線的持久面靜默失效。
// 病史：logout 路徑測試若非 mock 掉 markHasLoggedIn 就是完全不碰旗標＝零機器執法。
describe('touka_has_logged_in 持久性（登出不清；ADR-0144／G06）', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('clearAccessToken() 不得移除曾登入旗標（登出共用拆卸點）', () => {
    markHasLoggedIn();
    expect(hasEverLoggedIn()).toBe(true);
    clearAccessToken();
    expect(hasEverLoggedIn()).toBe(true);
  });

  it('logoutTouka() 之後曾登入旗標仍在（端到端登出路徑）', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: new Headers(),
    }));
    markHasLoggedIn();

    await logoutTouka();

    expect(hasEverLoggedIn()).toBe(true);
  });
});
