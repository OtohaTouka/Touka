/**
 * authService — P3 認證縫 #6：URL 收斂。
 *
 * login/refresh/logout 舊制固定打 CONFIG.PROXY_URL，業務請求與 changePassword 打
 * getActiveProxyUrl()——多連線設定下 refresh cookie 與 access token 面向分裂
 * （401 後對無 cookie 的 active origin 打 /auth/refresh → 誤殺 session）。本測試
 * 釘住三端點皆走 getActiveProxyUrl（單機同源未爆，但消除潛在不一致）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { authService } from '../authService';
import * as connections from '../../connections';

vi.mock('../../connections', () => ({
  fetchTouka: vi.fn().mockResolvedValue({ ok: true, json: async () => ({ access_token: 'synthetic-token', token_type: 'bearer' }) }),
  getActiveProxyUrl: vi.fn(() => 'http://active-origin'),
}));

const ACTIVE = 'http://active-origin';

describe('authService — P3 #6：URL 走 getActiveProxyUrl（不再固定 CONFIG.PROXY_URL）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('login 打 active origin', async () => {
    await authService.login('a@b.c', 'pw');
    expect(connections.fetchTouka).toHaveBeenCalledWith(
      ACTIVE, '/v1/auth/login', expect.anything());
  });

  it('refresh 打 active origin', async () => {
    await authService.refresh();
    expect(connections.fetchTouka).toHaveBeenCalledWith(
      ACTIVE, '/v1/auth/refresh', expect.anything());
  });

  it('logout 打 active origin', async () => {
    await authService.logout();
    expect(connections.fetchTouka).toHaveBeenCalledWith(
      ACTIVE, '/v1/auth/logout', expect.anything());
  });

  it('register 打 active origin', async () => {
    await authService.register('a@b.c', 'pw');
    expect(connections.fetchTouka).toHaveBeenCalledWith(
      ACTIVE, '/v1/auth/register', expect.anything());
  });
});
