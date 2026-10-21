/**
 * HAX G16（傳達使用者行動的後果）— 升級到最危險的 YOLO（全自動執行）
 * 時必須先二次確認；切換到其他較保守等級則立即套用、不打斷。
 *
 * 以 mock 取代 Radix 下拉與 confirmToast，聚焦在「選擇等級 → 是否確認」的決策。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { AuthSwitcher } from '@/components/chat/AuthSwitcher';
import zhTW from '@/locales/zh-TW/translation.json';
import en from '@/locales/en/translation.json';

const mockSetAuthLevel = vi.fn();
const mockConfirm = vi.fn();
let mockAuthLevel = 'Manual';
let mockIncognito = false;

vi.mock('@/lib/providers/ChatProvider', () => ({
  useChat: () => ({
    authLevel: mockAuthLevel,
    handleSetAuthLevel: mockSetAuthLevel,
    activeSessionId: 's1',
    isIncognito: mockIncognito,
  }),
}));

vi.mock('@/hooks/useConfirmToast', () => ({
  useConfirmToast: () => ({ confirmToast: mockConfirm }),
}));

// 將 Radix 下拉攤平為直接渲染（與 SessionItemDeletion 測試相同手法），
// 免去 portal/開合時序，專注驗證點擊各等級的行為。
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  DropdownMenuItem: ({ children, onClick }: any) => (
    <div data-testid="auth-item" onClick={onClick}>{children}</div>
  ),
}));

describe('AuthSwitcher（HAX G16 後果揭示）', () => {
  beforeEach(() => {
    mockSetAuthLevel.mockClear();
    mockConfirm.mockClear();
    mockAuthLevel = 'Manual';
    mockIncognito = false;
  });

  it('選擇 YOLO 會先要求確認，不立即套用', () => {
    render(<AuthSwitcher />);
    fireEvent.click(screen.getByText('YOLO'));
    expect(mockConfirm).toHaveBeenCalledTimes(1);
    expect(mockSetAuthLevel).not.toHaveBeenCalled();
  });

  it('確認後才真正套用 YOLO', () => {
    render(<AuthSwitcher />);
    fireEvent.click(screen.getByText('YOLO'));
    // confirmToast(message, onConfirm, label) → 取出 onConfirm 模擬使用者按下確認
    const onConfirm = mockConfirm.mock.calls[0][1] as () => void;
    onConfirm();
    expect(mockSetAuthLevel).toHaveBeenCalledWith('YOLO');
  });

  it('選擇較保守模式（Auto）會立即套用、不需確認', () => {
    render(<AuthSwitcher />);
    fireEvent.click(screen.getByText('Auto'));
    expect(mockSetAuthLevel).toHaveBeenCalledWith('Auto');
    expect(mockConfirm).not.toHaveBeenCalled();
  });

  it('只列三種模式，舊名稱不再是選項；看不懂的值顯示成 Manual', () => {
    mockAuthLevel = 'FullYOLO';
    render(<AuthSwitcher />);
    expect(screen.getAllByTestId('auth-item').map(item => item.textContent?.match(/^(Manual|Auto|YOLO)/)?.[0]))
      .toEqual(['Manual', 'Auto', 'YOLO']);
    expect(screen.queryByText('FullYOLO')).toBeNull();
    expect(screen.queryByText('SafeAuto')).toBeNull();
    expect(screen.queryByText('ReadOnly')).toBeNull();
    expect(screen.getAllByText('Manual')).toHaveLength(2); // 觸發按鈕＋選項
  });
});

describe('AuthSwitcher 無痕對話（ADR-0246 U2c-6）', () => {
  beforeEach(() => {
    mockSetAuthLevel.mockClear();
    mockConfirm.mockClear();
    mockIncognito = true;
  });

  it('鎖定顯示 Manual、沒有可切換的選項，並說明原因', () => {
    mockAuthLevel = 'YOLO';
    render(<AuthSwitcher />);
    expect(screen.queryAllByTestId('auth-item')).toHaveLength(0);
    expect(screen.queryByText('YOLO')).toBeNull();
    expect(screen.queryByText('Auto')).toBeNull();
    const locked = screen.getByRole('button', { name: 'Manual' });
    expect(locked).toBeDisabled();
    fireEvent.click(locked);
    expect(mockSetAuthLevel).not.toHaveBeenCalled();
    expect(screen.getByText('無痕對話無法追溯，只能用 Manual')).toBeInTheDocument();
  });

  it('說明照人類的理由寫（無法追溯），兩語同步（U2c-9）', () => {
    expect(zhTW.chat.auth_switcher.incognito_manual).toBe('無痕對話無法追溯，只能用 Manual');
    expect(en.chat.auth_switcher.incognito_manual).toBe(
      "Incognito chats can't be audited, so they always run in Manual",
    );
  });
});
