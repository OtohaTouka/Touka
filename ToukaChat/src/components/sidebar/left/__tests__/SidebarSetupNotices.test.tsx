import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import React from 'react';
import { SidebarSetupNotices } from '../SidebarSetupNotices';
import { SetupNoticesProvider } from '@/components/setup/SetupNoticesProvider';

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

vi.mock('@/hooks/useSetupStatus', () => ({
  useSetupStatus: vi.fn(),
}));

// 精靈本身另有測試；這裡只看「打開了沒、從哪一步開始、關掉時做什麼」。
vi.mock('@/components/setup/SetupWizard', () => ({
  SetupWizard: ({ initialStepId, onClose }: { initialStepId?: string; onClose: () => void }) => (
    <div data-testid="setup-wizard" data-initial-step={initialStepId ?? ''}>
      <button onClick={onClose}>close-wizard</button>
    </div>
  ),
}));

import { useAuth } from '@/contexts/AuthContext';
import { useSetupStatus, type SetupStatus } from '@/hooks/useSetupStatus';

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

const refetch = vi.fn();

function mockUser(role: string) {
  vi.mocked(useAuth).mockReturnValue({
    user: { role, email: 'u@test.com', id: '1', status: 'active' },
    isAuthenticated: true,
    isLoading: false,
    accessToken: null,
    login: vi.fn(),
    logout: vi.fn(),
    refreshUser: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>);
}

function mockStatus(status: SetupStatus | null) {
  vi.mocked(useSetupStatus).mockReturnValue({ status, isLoading: false, refetch });
}

function renderInProvider(ui: React.ReactNode = <SidebarSetupNotices />) {
  return render(<SetupNoticesProvider>{ui}</SetupNoticesProvider>);
}

describe('SidebarSetupNotices（左側邊欄的首次設定警告）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // 出現條件對照表：和舊的 TempPasswordBanner／SetupPromptBanner 相同。
  it.each([
    { name: '非管理員，即使四個問題都有', role: 'user', status: { ...cleanStatus(), is_admin_temp_password: true, dispatch_token_is_default: true, auth_secret_persisted: false, api_keys_configured: [] }, password: false, setup: false },
    { name: '管理員，狀態還沒讀到', role: 'admin', status: null, password: false, setup: false },
    { name: '管理員，全部設定好了', role: 'admin', status: cleanStatus(), password: false, setup: false },
    { name: '管理員，派工金鑰還是預設', role: 'admin', status: { ...cleanStatus(), dispatch_token_is_default: true }, password: false, setup: true },
    { name: '管理員，簽章金鑰沒存下來', role: 'admin', status: { ...cleanStatus(), auth_secret_persisted: false }, password: false, setup: true },
    { name: '管理員，沒有任何模型金鑰', role: 'admin', status: { ...cleanStatus(), api_keys_configured: [] }, password: false, setup: true },
    { name: '管理員，密碼還是臨時的（第一次登入）', role: 'admin', status: { ...cleanStatus(), is_admin_temp_password: true }, password: true, setup: true },
  ])('$name → 密碼警告 $password、設定警告 $setup', ({ role, status, password, setup }) => {
    mockUser(role);
    mockStatus(status);
    renderInProvider();
    expect(screen.queryByRole('alert') !== null).toBe(password);
    expect(screen.queryByRole('status') !== null).toBe(setup);
    if (password) expect(screen.getByRole('alert')).toHaveTextContent(/臨時/);
    if (setup) expect(screen.getByRole('status')).toHaveTextContent(/初次設定/);
  });

  it('沒有警告時完全不畫（不在側欄多留空白）', () => {
    mockUser('admin');
    mockStatus(cleanStatus());
    renderInProvider(<div data-testid="slot"><SidebarSetupNotices /></div>);
    expect(screen.getByTestId('slot')).toBeEmptyDOMElement();
  });

  it('沒有提供者時什麼都不畫', () => {
    const { container } = render(<SidebarSetupNotices />);
    expect(container).toBeEmptyDOMElement();
  });

  it('密碼警告不能關：卡片裡只有「變更密碼」一個按鈕', () => {
    mockUser('admin');
    mockStatus({ ...cleanStatus(), is_admin_temp_password: true });
    renderInProvider();
    const buttons = within(screen.getByRole('alert')).getAllByRole('button');
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveTextContent('變更密碼');
  });

  it('「變更密碼」直接打開精靈的管理員密碼步驟，並通知側欄（手機版關選單）', () => {
    mockUser('admin');
    mockStatus({ ...cleanStatus(), is_admin_temp_password: true });
    const onAction = vi.fn();
    renderInProvider(<SidebarSetupNotices onAction={onAction} />);
    expect(screen.queryByTestId('setup-wizard')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '變更密碼' }));
    expect(screen.getByTestId('setup-wizard')).toHaveAttribute('data-initial-step', 'admin_password');
    expect(onAction).toHaveBeenCalledTimes(1);
  });

  it('「立即設定」從精靈第一步開始，並通知側欄', () => {
    mockUser('admin');
    mockStatus({ ...cleanStatus(), dispatch_token_is_default: true });
    const onAction = vi.fn();
    renderInProvider(<SidebarSetupNotices onAction={onAction} />);
    fireEvent.click(screen.getByRole('button', { name: '立即設定' }));
    expect(screen.getByTestId('setup-wizard')).toHaveAttribute('data-initial-step', '');
    expect(onAction).toHaveBeenCalledTimes(1);
  });

  it('「稍後」只隱藏設定警告；側欄重新掛上（例如手機選單再打開）後仍隱藏', () => {
    mockUser('admin');
    mockStatus({ ...cleanStatus(), is_admin_temp_password: true });
    const { rerender } = renderInProvider();
    fireEvent.click(screen.getByRole('button', { name: /關閉初次設定提示/ }));
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole('alert')).toBeInTheDocument();

    rerender(<SetupNoticesProvider>{null}</SetupNoticesProvider>);
    expect(screen.queryByRole('alert')).toBeNull();
    rerender(<SetupNoticesProvider><SidebarSetupNotices /></SetupNoticesProvider>);
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });

  it('關掉精靈時重新讀狀態；問題都處理好時設定警告消失', () => {
    mockUser('admin');
    mockStatus({ ...cleanStatus(), api_keys_configured: [] });
    const { rerender } = renderInProvider();
    fireEvent.click(screen.getByRole('button', { name: '立即設定' }));
    fireEvent.click(screen.getByText('close-wizard'));
    expect(refetch).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('setup-wizard')).toBeNull();

    mockStatus(cleanStatus());
    rerender(<SetupNoticesProvider><SidebarSetupNotices /></SetupNoticesProvider>);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('重讀中或重讀失敗（狀態變回 null）時保留上次讀到的警告', () => {
    mockUser('admin');
    mockStatus({ ...cleanStatus(), is_admin_temp_password: true });
    const { rerender } = renderInProvider();
    expect(screen.getByRole('alert')).toBeInTheDocument();

    mockStatus(null);
    rerender(<SetupNoticesProvider><SidebarSetupNotices /></SetupNoticesProvider>);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('status')).toBeInTheDocument();
  });

  it('提供者在有警告時也不在頁面上多畫任何東西（不再變成側欄外的直欄）', () => {
    mockUser('admin');
    mockStatus({ ...cleanStatus(), is_admin_temp_password: true, api_keys_configured: [] });
    const { container } = render(
      <SetupNoticesProvider>
        <div data-testid="page">page</div>
      </SetupNoticesProvider>,
    );
    expect(container.childNodes).toHaveLength(1);
    expect(container.firstChild).toBe(screen.getByTestId('page'));
  });
});
