import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import { MainHeader } from '../MainHeader';
import { SetupNoticesProvider } from '@/components/setup/SetupNoticesProvider';

vi.mock('@/contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('@/hooks/useSetupStatus', () => ({ useSetupStatus: vi.fn() }));

import { useAuth } from '@/contexts/AuthContext';
import { useSetupStatus } from '@/hooks/useSetupStatus';

const props = {
  viewMode: "chat" as const,
  setViewMode: vi.fn(),
  isIDE: false,
  isMobile: false,
  onToggleSidebar: vi.fn(),
  isRightSidebarOpen: false,
  onToggleRightSidebar: vi.fn(),
  sysStatus: { mode: 'hybrid', status: 'online' },
};

function renderHeader(isTempPassword: boolean) {
  vi.mocked(useAuth).mockReturnValue({
    user: { role: 'admin', email: 'admin@test.com', id: '1', status: 'active' },
  } as unknown as ReturnType<typeof useAuth>);
  vi.mocked(useSetupStatus).mockReturnValue({
    status: {
      dispatch_token_is_default: false,
      auth_secret_persisted: true,
      api_keys_configured: ['anthropic'],
      is_admin_temp_password: isTempPassword,
      data_path_is_default: false,
      workspace_root_is_default: false,
      embedding_model: 'all-MiniLM-L6-v2',
    },
    isLoading: false,
    refetch: vi.fn(),
  });
  return render(<SetupNoticesProvider><MainHeader {...props} /></SetupNoticesProvider>);
}

describe('MainHeader — 側欄收合鈕的設定警告紅點', () => {
  it('有設定警告時，收合鈕上有紅點，按鈕名稱說明側欄裡有設定警告', () => {
    renderHeader(true);
    const toggle = screen.getByRole('button', { name: /設定警告/ });
    expect(toggle.querySelector('[data-testid="setup-notice-dot"]')).not.toBeNull();
  });

  it('沒有設定警告時沒有紅點，按鈕名稱照舊', () => {
    renderHeader(false);
    expect(screen.queryByTestId('setup-notice-dot')).toBeNull();
    expect(screen.getByRole('button', { name: '切換側邊欄' })).toBeInTheDocument();
  });
});
