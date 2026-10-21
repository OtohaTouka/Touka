import { describe, it, expect, beforeEach, vi } from 'vitest';

describe('P4 — Access token in-memory storage', () => {
  beforeEach(() => {
    // Reset module state between tests
  });

  it('getAccessToken / setAccessToken / clearAccessToken are exported', async () => {
    const mod = await import('@/lib/connections');
    expect(typeof mod.setAccessToken).toBe('function');
    expect(typeof mod.getAccessToken).toBe('function');
    expect(typeof mod.clearAccessToken).toBe('function');
  });

  it('setAccessToken stores token in memory, not localStorage', async () => {
    const mod = await import('@/lib/connections');
    const lsSetSpy = vi.spyOn(Storage.prototype, 'setItem');

    mod.setAccessToken('test-token-123');

    // localStorage.setItem must NOT be called for the auth token
    const tokenWrite = (lsSetSpy.mock.calls as [string, string][]).find(
      ([key]) => key === 'touka_auth_token',
    );
    expect(tokenWrite).toBeUndefined();

    // But getAccessToken must return the value
    expect(mod.getAccessToken()).toBe('test-token-123');
    mod.clearAccessToken();
    lsSetSpy.mockRestore();
  });

  it('clearAccessToken removes token from memory', async () => {
    const mod = await import('@/lib/connections');
    mod.setAccessToken('to-be-cleared');
    mod.clearAccessToken();
    expect(mod.getAccessToken()).toBeNull();
  });

  it('fetchTouka reads token from memory, not localStorage', async () => {
    const mod = await import('@/lib/connections');
    mod.setAccessToken('mem-token-abc');

    const lsGetSpy = vi.spyOn(Storage.prototype, 'getItem');
    const fetchSpy = vi.spyOn(global, 'fetch').mockResolvedValueOnce(
      new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );

    await mod.fetchTouka('http://localhost:8000', '/test');

    // localStorage.getItem must NOT have been called for touka_auth_token
    const tokenRead = (lsGetSpy.mock.calls as [string][]).find(
      ([key]) => key === 'touka_auth_token',
    );
    expect(tokenRead).toBeUndefined();

    mod.clearAccessToken();
    fetchSpy.mockRestore();
    lsGetSpy.mockRestore();
  });
});
