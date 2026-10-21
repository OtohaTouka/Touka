import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SetupPromptBanner } from '../SetupPromptBanner';

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

vi.mock('@/hooks/useSetupStatus', () => ({
  useSetupStatus: vi.fn(),
}));

import { useAuth } from '@/contexts/AuthContext';
import { useSetupStatus } from '@/hooks/useSetupStatus';
import type { SetupStatus } from '@/hooks/useSetupStatus';

function cleanStatus(): SetupStatus {
  return {
    dispatch_token_is_default: false,
    auth_secret_persisted: true,
    api_keys_configured: ['anthropic'],
    is_admin_temp_password: false,
    data_path_is_default: false,
    workspace_root_is_default: false,
    embedding_model: 'all-MiniLM-L6-v2',
  };
}

function mockAdmin() {
  vi.mocked(useAuth).mockReturnValue({
    user: { role: 'admin', email: 'admin@test.com', id: '1', status: 'active' },
    isAuthenticated: true,
    isLoading: false,
    accessToken: null,
    login: vi.fn(),
    logout: vi.fn(),
    refreshUser: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>);
}

describe('SetupPromptBanner', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('does not render when status is null', () => {
    mockAdmin();
    vi.mocked(useSetupStatus).mockReturnValue({
      status: null,
      isLoading: false,
      refetch: vi.fn(),
    });

    render(<SetupPromptBanner />);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('does not render when all setup conditions are clean', () => {
    mockAdmin();
    vi.mocked(useSetupStatus).mockReturnValue({
      status: cleanStatus(),
      isLoading: false,
      refetch: vi.fn(),
    });

    render(<SetupPromptBanner />);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('renders banner when dispatch_token_is_default is true', () => {
    mockAdmin();
    vi.mocked(useSetupStatus).mockReturnValue({
      status: { ...cleanStatus(), dispatch_token_is_default: true },
      isLoading: false,
      refetch: vi.fn(),
    });

    render(<SetupPromptBanner />);
    expect(screen.queryByRole('status')).not.toBeNull();
  });

  it('renders banner when auth_secret_persisted is false', () => {
    mockAdmin();
    vi.mocked(useSetupStatus).mockReturnValue({
      status: { ...cleanStatus(), auth_secret_persisted: false },
      isLoading: false,
      refetch: vi.fn(),
    });

    render(<SetupPromptBanner />);
    expect(screen.queryByRole('status')).not.toBeNull();
  });

  it('renders banner when api_keys_configured is empty', () => {
    mockAdmin();
    vi.mocked(useSetupStatus).mockReturnValue({
      status: { ...cleanStatus(), api_keys_configured: [] },
      isLoading: false,
      refetch: vi.fn(),
    });

    render(<SetupPromptBanner />);
    expect(screen.queryByRole('status')).not.toBeNull();
  });

  it('renders banner when is_admin_temp_password is true', () => {
    mockAdmin();
    vi.mocked(useSetupStatus).mockReturnValue({
      status: { ...cleanStatus(), is_admin_temp_password: true },
      isLoading: false,
      refetch: vi.fn(),
    });

    render(<SetupPromptBanner />);
    expect(screen.queryByRole('status')).not.toBeNull();
  });

  it('does not render for non-admin user even with setup issues', () => {
    vi.mocked(useAuth).mockReturnValue({
      user: { role: 'user', email: 'user@test.com', id: '2', status: 'active' },
      isAuthenticated: true,
      isLoading: false,
      accessToken: null,
      login: vi.fn(),
      logout: vi.fn(),
      refreshUser: vi.fn(),
    } as unknown as ReturnType<typeof useAuth>);
    vi.mocked(useSetupStatus).mockReturnValue({
      status: { ...cleanStatus(), dispatch_token_is_default: true },
      isLoading: false,
      refetch: vi.fn(),
    });

    render(<SetupPromptBanner />);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('hides the banner after clicking the Later button', () => {
    mockAdmin();
    vi.mocked(useSetupStatus).mockReturnValue({
      status: { ...cleanStatus(), dispatch_token_is_default: true },
      isLoading: false,
      refetch: vi.fn(),
    });

    render(<SetupPromptBanner />);
    expect(screen.queryByRole('status')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /關閉設定提示橫幅/ }));
    expect(screen.queryByRole('status')).toBeNull();
  });
});
