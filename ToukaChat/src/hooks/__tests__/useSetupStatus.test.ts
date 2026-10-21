import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useSetupStatus } from '../useSetupStatus';

// Mock dependencies
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(),
}));

vi.mock('@/lib/error_handler', () => ({
  logSSOTError: vi.fn(),
}));

import { useAuth } from '@/contexts/AuthContext';
import { fetchTouka } from '@/lib/connections';

const mockStatus = {
  dispatch_token_is_default: true,
  auth_secret_persisted: false,
  api_keys_configured: ['anthropic'],
  is_admin_temp_password: true,
  data_path_is_default: true,
  embedding_model: 'all-MiniLM-L6-v2',
};

describe('useSetupStatus', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns null status for non-admin users without calling API', async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: { role: 'normal', email: 'u@test.com', id: '1', status: 'active' },
      isAuthenticated: true, isLoading: false, accessToken: null,
      login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    });

    const { result } = renderHook(() => useSetupStatus());
    expect(result.current.status).toBeNull();
    expect(fetchTouka).not.toHaveBeenCalled();
  });

  it('calls /v1/admin/setup/status for admin users', async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: { role: 'admin', email: 'admin@test.com', id: '1', status: 'active' },
      isAuthenticated: true, isLoading: false, accessToken: null,
      login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    });
    vi.mocked(fetchTouka).mockResolvedValue({
      ok: true,
      json: async () => mockStatus,
    } as Response);

    const { result } = renderHook(() => useSetupStatus());

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(fetchTouka).toHaveBeenCalledWith('http://localhost:8000', '/v1/admin/setup/status');
    expect(result.current.status).toEqual(mockStatus);
  });

  it('refetch() triggers a second API call and returns updated status — F1 fix', async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: { role: 'admin', email: 'admin@test.com', id: '1', status: 'active' },
      isAuthenticated: true, isLoading: false, accessToken: null,
      login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    });
    vi.mocked(fetchTouka).mockResolvedValue({
      ok: true,
      json: async () => mockStatus,
    } as Response);

    const { result } = renderHook(() => useSetupStatus());

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(fetchTouka).toHaveBeenCalledTimes(1);
    expect(result.current.status).toEqual(mockStatus);

    // Call refetch — must trigger a second API request
    act(() => { result.current.refetch(); });

    await waitFor(() => expect(fetchTouka).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.status).toEqual(mockStatus));
  });

  it('refetch() resets status to null while re-fetching — F1 fix', async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: { role: 'admin', email: 'admin@test.com', id: '1', status: 'active' },
      isAuthenticated: true, isLoading: false, accessToken: null,
      login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    });
    // First call resolves immediately; second call we control manually
    let resolveSecond!: (v: Response) => void;
    const secondResponse = new Promise<Response>((res) => { resolveSecond = res; });
    vi.mocked(fetchTouka)
      .mockResolvedValueOnce({ ok: true, json: async () => mockStatus } as Response)
      .mockReturnValueOnce(secondResponse);

    const { result } = renderHook(() => useSetupStatus());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Trigger refetch — status should clear to null during re-fetch
    act(() => { result.current.refetch(); });

    await waitFor(() => expect(result.current.status).toBeNull());

    // Let the second fetch resolve
    resolveSecond({ ok: true, json: async () => mockStatus } as Response);
    await waitFor(() => expect(result.current.status).toEqual(mockStatus));
  });

  it('re-fetches when user.id changes while role stays admin — F2 fix', async () => {
    const adminUser = { role: 'admin', email: 'admin@test.com', id: 'user-1', status: 'active' };
    vi.mocked(useAuth).mockReturnValue({
      user: adminUser,
      isAuthenticated: true, isLoading: false, accessToken: null,
      login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    });
    vi.mocked(fetchTouka).mockResolvedValue({
      ok: true,
      json: async () => mockStatus,
    } as Response);

    const { result, rerender } = renderHook(() => useSetupStatus());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(fetchTouka).toHaveBeenCalledTimes(1);

    // Different admin logs in — same role, different id
    vi.mocked(useAuth).mockReturnValue({
      user: { ...adminUser, id: 'user-2' },
      isAuthenticated: true, isLoading: false, accessToken: null,
      login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    });
    rerender();

    await waitFor(() => expect(fetchTouka).toHaveBeenCalledTimes(2));
    expect(result.current.status).toEqual(mockStatus);
  });

  it('handles API error silently and returns null status', async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: { role: 'admin', email: 'admin@test.com', id: '1', status: 'active' },
      isAuthenticated: true, isLoading: false, accessToken: null,
      login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    });
    vi.mocked(fetchTouka).mockRejectedValue(new Error('Network error'));

    const { result } = renderHook(() => useSetupStatus());

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.status).toBeNull();
  });
});
