import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import LeftSidebar from '@/components/LeftSidebar';
import { SetupNoticesProvider } from '@/components/setup/SetupNoticesProvider';

vi.mock('@/contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('@/hooks/useSetupStatus', () => ({ useSetupStatus: vi.fn() }));
vi.mock('@/components/setup/SetupWizard', () => ({
  SetupWizard: () => <div data-testid="setup-wizard" />,
}));

vi.mock('@/lib/SystemStatusContext', () => ({
  useSystemStatus: () => ({ status: { mode: 'hybrid', memoryNotices: [], systemNotices: [] } }),
}));
vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({ sessions: [] }),
}));
vi.mock('@/lib/providers/ChatProvider', () => ({
  useChat: () => ({
    activeSessionId: null,
    handleNewSession: vi.fn(),
    handleSwitchSession: vi.fn(),
    handleSetHome: vi.fn(),
    handleSetProject: vi.fn(),
    handleUnsetHome: vi.fn(),
    streamingSessionIds: [],
  }),
}));
vi.mock('@/lib/providers/ProjectProvider', () => ({
  useProjects: () => ({ projects: [] }),
}));
vi.mock('@/hooks/useSessionGroups', () => ({
  useSessionGroups: () => ({ sessionGroups: {} }),
}));
vi.mock('@/hooks/useSessionActions', () => ({
  useSessionActions: () => ({}),
}));
vi.mock('@/components/sidebar/left/NavigationArea', () => ({
  NavigationArea: () => <nav data-testid="navigation-area" />,
}));

import { useAuth } from '@/contexts/AuthContext';
import { useSetupStatus } from '@/hooks/useSetupStatus';

function renderSidebar(onSetupAction = vi.fn()) {
  vi.mocked(useAuth).mockReturnValue({
    user: { role: 'admin', email: 'admin@test.com', id: '1', status: 'active' },
  } as unknown as ReturnType<typeof useAuth>);
  vi.mocked(useSetupStatus).mockReturnValue({
    status: {
      dispatch_token_is_default: false,
      auth_secret_persisted: true,
      api_keys_configured: [],
      is_admin_temp_password: true,
      data_path_is_default: false,
      workspace_root_is_default: false,
      embedding_model: 'all-MiniLM-L6-v2',
    },
    isLoading: false,
    refetch: vi.fn(),
  });
  render(<SetupNoticesProvider><LeftSidebar onSetupAction={onSetupAction} /></SetupNoticesProvider>);
  return onSetupAction;
}

describe('LeftSidebar — 首次設定警告放在側欄裡', () => {
  it('兩個警告在側欄裡、「新對話」按鈕上面', () => {
    renderSidebar();
    const aside = screen.getByRole('complementary');
    const alert = screen.getByRole('alert');
    const status = screen.getByRole('status');
    expect(aside).toContainElement(alert);
    expect(aside).toContainElement(status);
    const newChat = screen.getByTitle(/開新對話|新對話/);
    expect(alert.compareDocumentPosition(newChat) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('按警告的按鈕會呼叫 onSetupAction（手機版用來關選單）', () => {
    const onSetupAction = renderSidebar();
    fireEvent.click(screen.getByRole('button', { name: '變更密碼' }));
    expect(onSetupAction).toHaveBeenCalledTimes(1);
  });

  it('側欄太矮時整欄可以捲動、不會橫向捲動，對話紀錄區保留最小高度', () => {
    renderSidebar();
    const column = screen.getByRole('alert').closest('aside')?.firstElementChild as HTMLElement;
    expect(column.className).toContain('overflow-y-auto');
    expect(column.className).toContain('overflow-x-hidden');
    const sessionList = screen.getByRole('heading', { level: 2 }).closest('div.flex-grow') as HTMLElement;
    expect(sessionList.className.split(/\s+/)).toContain('min-h-12');
  });
});
