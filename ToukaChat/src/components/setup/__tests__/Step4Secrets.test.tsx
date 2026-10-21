import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { Step4Secrets } from '../steps/Step4Secrets';
import { fetchTouka } from '@/lib/connections';
import type { SetupStatus } from '@/hooks/useSetupStatus';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(),
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

const statusWith = (over: Partial<SetupStatus> = {}): SetupStatus => ({
  dispatch_token_is_default: false,
  auth_secret_persisted: true,
  api_keys_configured: [],
  is_admin_temp_password: false,
  data_path_is_default: false,
  workspace_root_is_default: false,
  embedding_model: 'all-MiniLM-L6-v2',
  ...over,
});

function openRotate() {
  fireEvent.click(screen.getByText(/進階：輪替密鑰/));
}

describe('Step4Secrets — 自動管理狀態 + 輪替', () => {
  beforeEach(() => vi.clearAllMocks());

  it('顯示已持久化狀態（✓）當密鑰已設定', () => {
    render(<Step4Secrets status={statusWith()} onNext={vi.fn()} />);
    expect(screen.getByText(/自動生成並持久化到 .env/)).toBeInTheDocument();
    expect(screen.getAllByText(/已自動生成並持久化/).length).toBeGreaterThanOrEqual(2);
  });

  it('顯示待生成狀態當密鑰尚未設定', () => {
    render(
      <Step4Secrets
        status={statusWith({ dispatch_token_is_default: true, auth_secret_persisted: false })}
        onNext={vi.fn()}
      />,
    );
    expect(screen.getAllByText(/首次啟動／登入後自動生成/).length).toBe(2);
  });

  it('渲染時不會自動呼叫後端（不再是必填輸入步驟）', () => {
    render(<Step4Secrets status={statusWith()} onNext={vi.fn()} />);
    expect(vi.mocked(fetchTouka)).not.toHaveBeenCalled();
  });

  it('輪替按鈕會 POST 產生的密鑰並顯示成功', async () => {
    vi.mocked(fetchTouka).mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    render(<Step4Secrets status={statusWith()} onNext={vi.fn()} />);
    openRotate();
    fireEvent.click(screen.getByRole('button', { name: /產生新密鑰並套用/ }));
    await waitFor(() => expect(screen.getByText(/已輪替並寫入/)).toBeInTheDocument());
    const options = vi.mocked(fetchTouka).mock.calls[0][2] as RequestInit;
    const body = JSON.parse(options.body as string);
    expect(body.dispatch_token).toBeTruthy();
    expect(body.auth_secret).toBeTruthy();
  });

  it('輪替失敗（res.ok=false）顯示錯誤、不崩潰', async () => {
    vi.mocked(fetchTouka).mockResolvedValue({
      ok: false,
      json: async () => ({ detail: 'boom' }),
    } as Response);
    render(<Step4Secrets status={statusWith()} onNext={vi.fn()} />);
    openRotate();
    fireEvent.click(screen.getByRole('button', { name: /產生新密鑰並套用/ }));
    await waitFor(() => expect(screen.getByText('boom')).toBeInTheDocument());
  });

  it('下一步按鈕前進、且不呼叫後端', () => {
    const onNext = vi.fn();
    render(<Step4Secrets status={statusWith()} onNext={onNext} />);
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    expect(onNext).toHaveBeenCalledOnce();
    expect(vi.mocked(fetchTouka)).not.toHaveBeenCalled();
  });
});
