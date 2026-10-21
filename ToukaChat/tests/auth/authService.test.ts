import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as connections from '../../src/lib/connections';
import { authService } from '../../src/lib/api/authService';
import { ERRORS } from '../../src/lib/error_codes';

describe('authService', () => {
  beforeEach(() => {
    vi.spyOn(connections, 'fetchTouka').mockResolvedValue({
      ok: true,
      json: vi.fn(),
      headers: { get: () => null }
    } as unknown as Response);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each([
    [401, { detail: { id: ERRORS.AUTH_INVALID_CREDENTIALS.id, message: 'private-sentinel' } }, 'AUTH_INVALID_CREDENTIALS'],
    [403, { detail: { code: 'AUTH_PERMISSION_DENIED', message: 'private-sentinel' } }, 'AUTH_PERMISSION_DENIED'],
    [429, { detail: 'private-sentinel' }, 'AUTH_RATE_LIMITED'],
    [422, { detail: [{ input: 'private-sentinel' }] }, 'VALIDATION_ERROR'],
    [500, { message: 'private-sentinel' }, 'SYSTEM_ERROR'],
    [401, { detail: 'private-sentinel' }, 'AUTH_INVALID_TOKEN'],
  ] as const)('classifies HTTP %s without reflecting payload', async (status, body, code) => {
    vi.mocked(connections.fetchTouka).mockResolvedValueOnce(new Response(JSON.stringify(body), { status }));
    const error = await authService.login('test@touka.io', 'synthetic-password').catch(value => value);
    expect(error).toMatchObject({ code });
    expect(JSON.stringify(error)).not.toContain('private-sentinel');
    expect(error.message).not.toContain('private-sentinel');
    expect(connections.fetchTouka).toHaveBeenCalledTimes(1);
  });

  it.each([null, {}, { access_token: '', token_type: 'bearer' }, { access_token: 'token', token_type: 'other' }])('rejects invalid login success %j', async body => {
    vi.mocked(connections.fetchTouka).mockResolvedValueOnce(new Response(JSON.stringify(body)));
    await expect(authService.login('test@touka.io', 'synthetic-password')).rejects.toMatchObject({ code: 'AUTH_INVALID_TOKEN' });
  });

  it('classifies non-JSON and network failures without echoing exception contents', async () => {
    vi.mocked(connections.fetchTouka).mockResolvedValueOnce(new Response('private-sentinel', { status: 500 }));
    await expect(authService.login('test@touka.io', 'synthetic-password')).rejects.toMatchObject({ code: 'SYSTEM_ERROR' });
    vi.mocked(connections.fetchTouka).mockRejectedValueOnce(new Error('private-sentinel'));
    const error = await authService.login('test@touka.io', 'synthetic-password').catch(value => value);
    expect(error).toMatchObject({ code: 'NETWORK_CONNECTION_ERROR' });
    expect(error.message).not.toContain('private-sentinel');
  });

  it('login should call fetchTouka with correct path and body', async () => {
    vi.mocked(connections.fetchTouka).mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ access_token: 'fake-access', token_type: 'bearer' }),
      headers: { get: () => null }
    } as unknown as Response);

    const result = await authService.login('test@touka.io', 'pass');

    expect(connections.fetchTouka).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/auth/login',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ email: 'test@touka.io', password: 'pass' })
      })
    );
    expect(result.access_token).toBe('fake-access');
  });

  it('refresh should call fetchTouka with POST method', async () => {
    vi.mocked(connections.fetchTouka).mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ access_token: 'new-access', token_type: 'bearer' }),
      headers: { get: () => null }
    } as unknown as Response);

    const result = await authService.refresh();

    expect(connections.fetchTouka).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/auth/refresh',
      expect.objectContaining({ method: 'POST' })
    );
    expect(result.access_token).toBe('new-access');
  });

  it('register should call fetchTouka with correct body', async () => {
    vi.mocked(connections.fetchTouka).mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ id: '123', email: 'test@touka.io', role: 'normal' }),
      headers: { get: () => null }
    } as unknown as Response);

    const result = await authService.register('test@touka.io', 'pass');

    expect(connections.fetchTouka).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/auth/register',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ email: 'test@touka.io', password: 'pass', role: 'normal' })
      })
    );
    expect(result.email).toBe('test@touka.io');
  });

  it('logout should call fetchTouka with POST method and return json data on success', async () => {
    vi.mocked(connections.fetchTouka).mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ status: 'success', message: 'Logged out successfully' }),
      headers: { get: () => null }
    } as unknown as Response);

    const result = await authService.logout();

    expect(connections.fetchTouka).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/auth/logout',
      expect.objectContaining({ method: 'POST' })
    );
    expect(result.status).toBe('success');
  });

  it('logout should gracefully handle errors and return success status to prevent blocking frontend cleanup', async () => {
    // 模擬後端回傳 401 或出錯 (ok: false)
    vi.mocked(connections.fetchTouka).mockResolvedValueOnce({
      ok: false,
      json: () => Promise.resolve({ detail: 'Token expired' }),
      headers: { get: () => null }
    } as unknown as Response);

    const result = await authService.logout();

    expect(connections.fetchTouka).toHaveBeenCalled();
    expect(result.status).toBe('success');

    // 模擬網絡崩潰/拋出 Exception 的情況
    vi.mocked(connections.fetchTouka).mockRejectedValueOnce(new Error('Network error'));

    const resultNetworkError = await authService.logout();
    expect(resultNetworkError.status).toBe('success');
  });
});
