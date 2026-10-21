import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, renderHook, waitFor } from '@testing-library/react';
import { AuthProvider, useAuth } from '../../src/contexts/AuthContext';
import { authService } from '../../src/lib/api/authService';
import * as connections from '../../src/lib/connections';

// AuthContext 現在使用 useRouter()，需 mock next/navigation
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => '/',
}));

vi.mock('../../src/lib/api/authService', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/lib/api/authService')>(),
  authService: {
    refresh: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
  }
}));

// AuthContext.logout() 呼叫 logoutTouka，需在模組層級 mock connections
vi.mock('../../src/lib/connections', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/lib/connections')>(),
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn().mockResolvedValue({ ok: false }),
  logoutTouka: vi.fn(),
  // P4 fix: in-memory token management
  // 修復 2/3（ADR-0144）新增介面
  setSessionExpiredHandler: vi.fn(),
  markHasLoggedIn: vi.fn(),
}));

const TestComponent = () => {
  const { isAuthenticated, isLoading, login, logout } = useAuth();
  
  if (isLoading) return <div>Loading...</div>;
  
  return (
    <div>
      <div data-testid="status">{isAuthenticated ? 'Logged In' : 'Logged Out'}</div>
      <button onClick={() => login('test@test.com', 'pass')}>Login</button>
      <button onClick={logout}>Logout</button>
    </div>
  );
};

const syntheticToken = `e30.${btoa(JSON.stringify({ sub: 'u1', type: 'access', exp: 9999999999 }))}.synthetic`;

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <AuthProvider>{children}</AuthProvider>
);

// /auth/me 成功回應（含使用者），供「登入成功」路徑使用
const okProfile = () =>
  vi.mocked(connections.fetchTouka).mockResolvedValue(
    { ok: true, json: async () => ({ id: 'u1', email: 'test@test.com', role: 'normal' }) } as unknown as Response
  );

describe('AuthContext', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('should show loading initially and attempt to refresh token', async () => {
    vi.mocked(authService.refresh).mockResolvedValueOnce({ access_token: syntheticToken, token_type: 'bearer' });
    okProfile();

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    // Initially it should show loading (since we are doing an async refresh on mount)
    expect(screen.getByText('Loading...')).toBeInTheDocument();
    
    // After refresh completes, it should show Logged In
    const status = await screen.findByTestId('status');
    expect(status.textContent).toBe('Logged In');
    expect(authService.refresh).toHaveBeenCalledTimes(1);
  });

  it('should show logged out if refresh fails on mount', async () => {
    vi.mocked(authService.refresh).mockRejectedValueOnce(new Error('No token'));
    
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    const status = await screen.findByTestId('status');
    expect(status.textContent).toBe('Logged Out');
  });

  it('should update state after successful login', async () => {
    vi.mocked(authService.refresh).mockRejectedValueOnce(new Error('No initial token'));
    vi.mocked(authService.login).mockResolvedValueOnce({ access_token: syntheticToken, token_type: 'bearer' });
    okProfile();

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await screen.findByText('Logged Out'); // wait for mount refresh to fail

    // Act
    act(() => {
      screen.getByText('Login').click();
    });

    const status = await screen.findByTestId('status');
    expect(status.textContent).toBe('Logged In');
    expect(authService.login).toHaveBeenCalledWith('test@test.com', 'pass', {
      proxyUrl: 'http://localhost:8000', signal: expect.any(AbortSignal), credentialEpoch: expect.any(Number),
    });
  });

  it('should clear state on logout', async () => {
    vi.mocked(authService.refresh).mockResolvedValueOnce({ access_token: syntheticToken, token_type: 'bearer' });
    okProfile();
    // logout() 使用 logoutTouka()，mock connections 模組
    vi.mocked(connections.logoutTouka).mockResolvedValueOnce(undefined);

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await screen.findByText('Logged In');

    act(() => {
      screen.getByText('Logout').click();
    });

    const status = await screen.findByTestId('status');
    expect(status.textContent).toBe('Logged Out');
    expect(connections.logoutTouka).toHaveBeenCalledTimes(1);
  });

  it('initAuth: refresh 成功但 /auth/me 失敗時維持登出（不殘留 isAuthenticated=true 但 user=null）', async () => {
    vi.mocked(authService.refresh).mockResolvedValueOnce({ access_token: syntheticToken, token_type: 'bearer' });
    vi.mocked(connections.fetchTouka).mockResolvedValue({ ok: false } as unknown as Response);

    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.user).toBeNull();
  });

  it('login: 登入成功但 /auth/me 失敗時拋錯並回滾（不殘留 isAuthenticated=true 但 user=null）', async () => {
    vi.mocked(authService.refresh).mockRejectedValueOnce(new Error('no session'));
    vi.mocked(authService.login).mockResolvedValueOnce({ access_token: syntheticToken, token_type: 'bearer' });
    vi.mocked(connections.fetchTouka).mockResolvedValue({ ok: false } as unknown as Response);

    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      await expect(result.current.login('a@b.com', 'pw')).rejects.toThrow();
    });

    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.user).toBeNull();
  });
});
