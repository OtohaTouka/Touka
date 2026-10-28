/**
 * MainAreaControls — 對話區角落的按鈕（上導覽列拿掉後的替代）
 *
 * 1. 左側邊欄收起時，左上角才有「展開側邊欄」；有首次設定警告時鈕上有紅點、名稱說明有警告。
 * 2. 右上角「工具面板」用 aria-expanded 表示右側邊欄開或關，並指向右側面板。
 * 3. IDE 沒有左側邊欄：只有設定鈕（aria-pressed），沒有展開鈕與工具面板鈕。
 * 4. 側欄剛收起而焦點掉了時，焦點交給展開鈕；一開始就收起時不搶焦點。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { MainAreaControls } from '../MainAreaControls';
import { SetupNoticesProvider } from '@/components/setup/SetupNoticesProvider';

vi.mock('@/contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('@/hooks/useSetupStatus', () => ({ useSetupStatus: vi.fn() }));

import { useAuth } from '@/contexts/AuthContext';
import { useSetupStatus } from '@/hooks/useSetupStatus';

type Props = React.ComponentProps<typeof MainAreaControls>;

function makeProps(overrides: Partial<Props> = {}): Props {
  return {
    isIDE: false,
    isLeftSidebarCollapsed: false,
    onExpandLeftSidebar: vi.fn(),
    isRightSidebarOpen: false,
    onToggleRightSidebar: vi.fn(),
    isSettingsOpen: false,
    onToggleSettings: vi.fn(),
    takeFocusRequest: vi.fn().mockReturnValue(true),
    ...overrides,
  };
}

function withSetupWarning(isTempPassword: boolean, ui: React.ReactElement) {
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
  return <SetupNoticesProvider>{ui}</SetupNoticesProvider>;
}

describe('MainAreaControls — 左上角展開鈕', () => {
  beforeEach(() => vi.clearAllMocks());

  it('左側邊欄展開時沒有展開鈕', () => {
    render(<MainAreaControls {...makeProps()} />);
    expect(screen.queryByRole('button', { name: '展開側邊欄' })).toBeNull();
  });

  it('左側邊欄收起時有展開鈕，按了呼叫 onExpandLeftSidebar', () => {
    const props = makeProps({ isLeftSidebarCollapsed: true });
    render(<MainAreaControls {...props} />);
    const button = screen.getByRole('button', { name: '展開側邊欄' });
    expect(button).toHaveAttribute('title', '展開側邊欄');
    fireEvent.click(button);
    expect(props.onExpandLeftSidebar).toHaveBeenCalledTimes(1);
  });

  it('有設定警告時，展開鈕上有紅點，按鈕名稱說明側欄裡有設定警告', () => {
    render(withSetupWarning(true, <MainAreaControls {...makeProps({ isLeftSidebarCollapsed: true })} />));
    const button = screen.getByRole('button', { name: /設定警告/ });
    expect(button.querySelector('[data-testid="setup-notice-dot"]')).not.toBeNull();
  });

  it('沒有設定警告時沒有紅點，按鈕名稱照舊', () => {
    render(withSetupWarning(false, <MainAreaControls {...makeProps({ isLeftSidebarCollapsed: true })} />));
    expect(screen.queryByTestId('setup-notice-dot')).toBeNull();
    expect(screen.getByRole('button', { name: '展開側邊欄' })).toBeInTheDocument();
  });
});

describe('MainAreaControls — 右上角工具面板鈕', () => {
  beforeEach(() => vi.clearAllMocks());

  it('aria-expanded 跟著右側邊欄開關，並指向右側面板', () => {
    const { rerender } = render(<MainAreaControls {...makeProps()} />);
    const button = screen.getByRole('button', { name: '工具面板' });
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(button).toHaveAttribute('aria-controls', 'right-sidebar-panel');
    expect(button).toHaveAttribute('title', '工具面板');
    rerender(<MainAreaControls {...makeProps({ isRightSidebarOpen: true })} />);
    expect(screen.getByRole('button', { name: '工具面板' })).toHaveAttribute('aria-expanded', 'true');
  });

  it('按了呼叫 onToggleRightSidebar', () => {
    const props = makeProps();
    render(<MainAreaControls {...props} />);
    fireEvent.click(screen.getByRole('button', { name: '工具面板' }));
    expect(props.onToggleRightSidebar).toHaveBeenCalledTimes(1);
  });

  it('非 IDE 沒有設定鈕（設定在左側邊欄）', () => {
    render(<MainAreaControls {...makeProps()} />);
    expect(screen.queryByRole('button', { name: '設定' })).toBeNull();
  });
});

describe('MainAreaControls — IDE 模式', () => {
  beforeEach(() => vi.clearAllMocks());

  it('只有設定鈕，沒有展開鈕與工具面板鈕', () => {
    render(<MainAreaControls {...makeProps({ isIDE: true, isLeftSidebarCollapsed: true })} />);
    expect(screen.queryByRole('button', { name: '展開側邊欄' })).toBeNull();
    expect(screen.queryByRole('button', { name: '工具面板' })).toBeNull();
    expect(screen.getAllByRole('button')).toHaveLength(1);
  });

  it('設定鈕的 aria-pressed 跟著設定頁開關，按了呼叫 onToggleSettings', () => {
    const props = makeProps({ isIDE: true });
    const { rerender } = render(<MainAreaControls {...props} />);
    const button = screen.getByRole('button', { name: '設定' });
    expect(button).toHaveAttribute('aria-pressed', 'false');
    expect(button).toHaveAttribute('title', '設定');
    fireEvent.click(button);
    expect(props.onToggleSettings).toHaveBeenCalledTimes(1);
    rerender(<MainAreaControls {...makeProps({ isIDE: true, isSettingsOpen: true })} />);
    expect(screen.getByRole('button', { name: '設定' })).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('MainAreaControls — 鍵盤焦點', () => {
  beforeEach(() => vi.clearAllMocks());

  it('側欄剛收起、焦點掉在 body 時，焦點交給展開鈕', () => {
    const { rerender } = render(<MainAreaControls {...makeProps()} />);
    (document.activeElement as HTMLElement | null)?.blur();
    rerender(<MainAreaControls {...makeProps({ isLeftSidebarCollapsed: true })} />);
    expect(screen.getByRole('button', { name: '展開側邊欄' })).toHaveFocus();
  });

  it('側欄剛收起、焦點還在左側面板裡（看不見了）時，焦點交給展開鈕', () => {
    const panel = document.createElement('div');
    panel.id = 'left-sidebar-panel';
    const collapseButton = document.createElement('button');
    panel.appendChild(collapseButton);
    document.body.appendChild(panel);
    try {
      const { rerender } = render(<MainAreaControls {...makeProps()} />);
      collapseButton.focus();
      rerender(<MainAreaControls {...makeProps({ isLeftSidebarCollapsed: true })} />);
      expect(screen.getByRole('button', { name: '展開側邊欄' })).toHaveFocus();
    } finally {
      panel.remove();
    }
  });

  it('焦點在別的地方（例如輸入框）時不搶焦點', () => {
    const input = document.createElement('input');
    document.body.appendChild(input);
    try {
      const { rerender } = render(<MainAreaControls {...makeProps()} />);
      input.focus();
      rerender(<MainAreaControls {...makeProps({ isLeftSidebarCollapsed: true })} />);
      expect(input).toHaveFocus();
    } finally {
      input.remove();
    }
  });

  it('一開始就收起時不搶焦點', () => {
    (document.activeElement as HTMLElement | null)?.blur();
    render(<MainAreaControls {...makeProps({ isLeftSidebarCollapsed: true })} />);
    expect(screen.getByRole('button', { name: '展開側邊欄' })).not.toHaveFocus();
  });

  it('這次收起不是按鈕要求的（面板掛上時自己同步狀態）就不搶焦點', () => {
    // 載入時焦點在 body，若照樣交給展開鈕，「第一下 Tab 跳過側欄」就失效，螢幕閱讀器也會先唸這顆鈕。
    const takeFocusRequest = vi.fn().mockReturnValue(false);
    const { rerender } = render(<MainAreaControls {...makeProps({ takeFocusRequest })} />);
    (document.activeElement as HTMLElement | null)?.blur();
    rerender(<MainAreaControls {...makeProps({ isLeftSidebarCollapsed: true, takeFocusRequest })} />);
    expect(takeFocusRequest).toHaveBeenCalledWith(false);
    expect(screen.getByRole('button', { name: '展開側邊欄' })).not.toHaveFocus();
  });
});
