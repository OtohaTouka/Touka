import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SetupWizard } from '../SetupWizard';

// Mock all dependencies to avoid real API calls and module imports
vi.mock('@/contexts/AuthContext', () => ({ useAuth: vi.fn(() => ({ user: { role: 'admin' } })) }));
vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ status: 'ok' }) })),
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));
vi.mock('@/hooks/useSetupStatus', () => ({ useSetupStatus: vi.fn(() => ({ status: null, isLoading: false, refetch: vi.fn() })) }));

const mockStatus = {
  dispatch_token_is_default: false,
  auth_secret_persisted: true,
  api_keys_configured: ['anthropic'],
  is_admin_temp_password: false,
  data_path_is_default: false,
  workspace_root_is_default: false,
  embedding_model: 'all-MiniLM-L6-v2',
};

describe('SetupWizard', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('renders the first step on mount', () => {
    render(<SetupWizard status={mockStatus} onClose={vi.fn()} />);
    expect(screen.getByText('初次設定')).toBeInTheDocument();
    expect(screen.getByText(/步驟 1/)).toBeInTheDocument();
  });

  it('advances to next step when Next is clicked on step 1', () => {
    render(<SetupWizard status={mockStatus} onClose={vi.fn()} />);
    const nextBtn = screen.getByRole('button', { name: /下一步/ });
    fireEvent.click(nextBtn);
    expect(screen.getByText(/步驟 2/)).toBeInTheDocument();
  });

  it('calls onClose when × button is clicked', () => {
    const onClose = vi.fn();
    render(<SetupWizard status={mockStatus} onClose={onClose} />);
    const closeBtn = screen.getByLabelText('關閉設定精靈');
    fireEvent.click(closeBtn);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('goes back to the previous step when Back is clicked', () => {
    render(<SetupWizard status={mockStatus} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
    expect(screen.getByText(/步驟 2/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /上一步/ }));
    expect(screen.getByText(/步驟 1/)).toBeInTheDocument();
  });

  it('starts at the given step when initialStepId is set (sidebar「變更密碼」)', () => {
    render(<SetupWizard status={mockStatus} initialStepId="admin_password" onClose={vi.fn()} />);
    expect(screen.getByText(/步驟 2/)).toBeInTheDocument();
    expect(screen.getByLabelText('新密碼')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /上一步/ }));
    expect(screen.getByText(/步驟 1/)).toBeInTheDocument();
  });

  it('falls back to the first step when initialStepId is unknown', () => {
    render(<SetupWizard status={mockStatus} initialStepId={'no_such_step' as never} onClose={vi.fn()} />);
    expect(screen.getByText(/步驟 1/)).toBeInTheDocument();
  });

  it('does not show a Back button on the first step', () => {
    render(<SetupWizard status={mockStatus} onClose={vi.fn()} />);
    expect(screen.queryByRole('button', { name: /上一步/ })).toBeNull();
  });

  it('前進到「完成」時重讀設定狀態（第 7 步不能用精靈打開時的舊狀態）', () => {
    const onStatusRefresh = vi.fn();
    render(<SetupWizard status={mockStatus} initialStepId="create_user" onClose={vi.fn()} onStatusRefresh={onStatusRefresh} />);
    fireEvent.click(screen.getByRole('button', { name: '略過 — 結束設定' }));
    expect(screen.getByText(/步驟 7/)).toBeInTheDocument();
    expect(onStatusRefresh).toHaveBeenCalledOnce();
  });

  it('其他步驟之間前進不重讀設定狀態', () => {
    const onStatusRefresh = vi.fn();
    render(<SetupWizard status={mockStatus} onClose={vi.fn()} onStatusRefresh={onStatusRefresh} />);
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
    expect(screen.getByText(/步驟 2/)).toBeInTheDocument();
    expect(onStatusRefresh).not.toHaveBeenCalled();
  });

  it('uses a scrollable overlay with m-auto card (tall steps stay reachable)', () => {
    // 回歸測試（實際事故）：金鑰步驟 7 個供應商輸入使卡片高於視窗時，
    // flex 置中會裁掉上下兩端且無法捲動 → 「下一步」按鈕點不到。
    // 正確版面＝遮罩 overflow-y-auto ＋ 卡片 m-auto（矮＝置中、高＝可捲到底）。
    const { container } = render(<SetupWizard status={mockStatus} onClose={vi.fn()} />);
    const overlay = container.firstElementChild as HTMLElement;
    expect(overlay.className).toContain('overflow-y-auto');
    expect(overlay.className).not.toContain('items-center');
    const card = overlay.firstElementChild as HTMLElement;
    expect(card.className).toContain('m-auto');
  });
});
