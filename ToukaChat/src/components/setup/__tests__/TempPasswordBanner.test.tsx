import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TempPasswordBanner } from '../TempPasswordBanner';

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

vi.mock('@/hooks/useSetupStatus', () => ({
  useSetupStatus: vi.fn(),
}));

import { useAuth } from '@/contexts/AuthContext';
import { useSetupStatus } from '@/hooks/useSetupStatus';

describe('TempPasswordBanner', () => {
  it('renders banner when admin has temp password', () => {
    vi.mocked(useAuth).mockReturnValue({
      user: { role: 'admin', email: 'admin@test.com', id: '1', status: 'active' },
      isAuthenticated: true, isLoading: false, accessToken: null,
      login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    });
    vi.mocked(useSetupStatus).mockReturnValue({
      status: {
        is_admin_temp_password: true,
        dispatch_token_is_default: false,
        auth_secret_persisted: true,
        api_keys_configured: ['anthropic'],
        data_path_is_default: false,
        workspace_root_is_default: false,
        embedding_model: 'all-MiniLM-L6-v2',
      },
      isLoading: false,
      refetch: vi.fn(),
    });

    render(<TempPasswordBanner />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText(/臨時預設值/)).toBeInTheDocument();
  });

  it('does not render when is_admin_temp_password is false', () => {
    vi.mocked(useAuth).mockReturnValue({
      user: { role: 'admin', email: 'admin@test.com', id: '1', status: 'active' },
      isAuthenticated: true, isLoading: false, accessToken: null,
      login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    });
    vi.mocked(useSetupStatus).mockReturnValue({
      status: {
        is_admin_temp_password: false,
        dispatch_token_is_default: false,
        auth_secret_persisted: true,
        api_keys_configured: ['anthropic'],
        data_path_is_default: false,
        workspace_root_is_default: false,
        embedding_model: 'all-MiniLM-L6-v2',
      },
      isLoading: false,
      refetch: vi.fn(),
    });

    const { container } = render(<TempPasswordBanner />);
    expect(container.firstChild).toBeNull();
  });

  it('does not render for non-admin users', () => {
    vi.mocked(useAuth).mockReturnValue({
      user: { role: 'normal', email: 'u@test.com', id: '2', status: 'active' },
      isAuthenticated: true, isLoading: false, accessToken: null,
      login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    });
    vi.mocked(useSetupStatus).mockReturnValue({
      status: { is_admin_temp_password: true, dispatch_token_is_default: false, auth_secret_persisted: true, api_keys_configured: [], data_path_is_default: false, workspace_root_is_default: false, embedding_model: 'all-MiniLM-L6-v2' },
      isLoading: false,
      refetch: vi.fn(),
    });

    const { container } = render(<TempPasswordBanner />);
    expect(container.firstChild).toBeNull();
  });

  it('renders Change Password button when onChangePassword is provided', () => {
    vi.mocked(useAuth).mockReturnValue({
      user: { role: 'admin', email: 'admin@test.com', id: '1', status: 'active' },
      isAuthenticated: true, isLoading: false, accessToken: null,
      login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    });
    vi.mocked(useSetupStatus).mockReturnValue({
      status: { is_admin_temp_password: true, dispatch_token_is_default: false, auth_secret_persisted: true, api_keys_configured: [], data_path_is_default: false, workspace_root_is_default: false, embedding_model: 'all-MiniLM-L6-v2' },
      isLoading: false,
      refetch: vi.fn(),
    });

    const onChangePassword = vi.fn();
    render(<TempPasswordBanner onChangePassword={onChangePassword} />);
    expect(screen.getByText('變更密碼')).toBeInTheDocument();
  });
});
