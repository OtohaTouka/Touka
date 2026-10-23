import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { Step2AdminPassword } from '../steps/Step2AdminPassword';
import { fetchTouka } from '@/lib/connections';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(),
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

function fillAndSubmit(pw: string, confirm: string) {
  fireEvent.change(screen.getByLabelText('新密碼'), { target: { value: pw } });
  fireEvent.change(screen.getByLabelText('確認密碼'), { target: { value: confirm } });
  fireEvent.click(screen.getByRole('button', { name: /設定密碼/ }));
}

describe('Step2AdminPassword — F4: password complexity', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('shows the password requirements checklist on mount (rules upfront)', () => {
    render(<Step2AdminPassword status={null} onNext={vi.fn()} onPasswordChanged={vi.fn()} />);
    expect(screen.getByText('密碼需求')).toBeInTheDocument();
    expect(screen.getByText('至少 8 個字元')).toBeInTheDocument();
  });

  it('rejects password without uppercase letter — F4 fix', () => {
    render(<Step2AdminPassword status={null} onNext={vi.fn()} onPasswordChanged={vi.fn()} />);
    fillAndSubmit('abc123!xyz', 'abc123!xyz');
    expect(screen.getByText(/密碼至少需一個大寫/)).toBeInTheDocument();
  });

  it('rejects password without lowercase letter — F4 fix', () => {
    render(<Step2AdminPassword status={null} onNext={vi.fn()} onPasswordChanged={vi.fn()} />);
    fillAndSubmit('ABC123!XYZ', 'ABC123!XYZ');
    expect(screen.getByText(/密碼至少需一個小寫/)).toBeInTheDocument();
  });

  it('rejects password without digit — F4 fix', () => {
    render(<Step2AdminPassword status={null} onNext={vi.fn()} onPasswordChanged={vi.fn()} />);
    fillAndSubmit('Abcdefgh!', 'Abcdefgh!');
    expect(screen.getByText(/密碼至少需一個數字/)).toBeInTheDocument();
  });

  it('rejects password without special character — F4 fix', () => {
    render(<Step2AdminPassword status={null} onNext={vi.fn()} onPasswordChanged={vi.fn()} />);
    fillAndSubmit('Abcdefg1', 'Abcdefg1');
    expect(screen.getByText(/密碼至少需一個特殊/)).toBeInTheDocument();
  });

  it('rejects password shorter than 8 characters — regression guard', () => {
    render(<Step2AdminPassword status={null} onNext={vi.fn()} onPasswordChanged={vi.fn()} />);
    fillAndSubmit('Ab1!', 'Ab1!');
    expect(screen.getByText(/密碼至少需 8/)).toBeInTheDocument();
  });

  it('passes validation for a strong password — no complexity error shown', () => {
    render(<Step2AdminPassword status={null} onNext={vi.fn()} onPasswordChanged={vi.fn()} />);
    fillAndSubmit('Strong#Pass1', 'Strong#Pass1');
    expect(screen.queryByText(/密碼至少/)).not.toBeInTheDocument();
  });
});

const tempStatus = {
  dispatch_token_is_default: true,
  auth_secret_persisted: false,
  api_keys_configured: [],
  is_admin_temp_password: true,
  data_path_is_default: true,
  workspace_root_is_default: true,
  embedding_model: 'all-MiniLM-L6-v2',
};

describe('Step2AdminPassword — 改完密碼與保留目前密碼（ADR-0053 補記 2026-10-23）', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it.each([
    ['臨時密碼', false, tempStatus],
    ['狀態還沒讀到', false, null],
    ['已經不是臨時密碼', true, { ...tempStatus, is_admin_temp_password: false }],
  ])('%s：顯示「保留目前密碼」＝%s', (_label, shown, status) => {
    render(<Step2AdminPassword status={status} onNext={vi.fn()} onPasswordChanged={vi.fn()} />);
    expect(!!screen.queryByRole('button', { name: '保留目前密碼' })).toBe(shown);
    expect(!!screen.queryByText(/管理員密碼已經設定過/)).toBe(shown);
    expect(!!screen.queryByText(/以臨時密碼建立/)).toBe(!shown);
  });

  it('按「保留目前密碼」直接到下一步，不打後端', () => {
    const onNext = vi.fn();
    const onPasswordChanged = vi.fn();
    render(<Step2AdminPassword status={{ ...tempStatus, is_admin_temp_password: false }}
      onNext={onNext} onPasswordChanged={onPasswordChanged} />);
    fireEvent.click(screen.getByRole('button', { name: '保留目前密碼' }));
    expect(onNext).toHaveBeenCalledOnce();
    expect(onPasswordChanged).not.toHaveBeenCalled();
    expect(fetchTouka).not.toHaveBeenCalled();
  });

  it('改密碼成功：回報被改密碼的使用者，不直接前進（舊卡已被後端撤銷）', async () => {
    vi.mocked(fetchTouka)
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'u1' }), { status: 200 }))
      .mockResolvedValueOnce(new Response('{}', { status: 200 }));
    const onNext = vi.fn();
    const onPasswordChanged = vi.fn();
    render(<Step2AdminPassword status={tempStatus} onNext={onNext} onPasswordChanged={onPasswordChanged} />);
    fillAndSubmit('Strong#Pass1', 'Strong#Pass1');
    await waitFor(() => expect(onPasswordChanged).toHaveBeenCalledWith('u1'));
    expect(onNext).not.toHaveBeenCalled();
  });

  it('改密碼失敗：不回報、不前進', async () => {
    vi.mocked(fetchTouka)
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'u1' }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: '不行' }), { status: 422 }));
    const onNext = vi.fn();
    const onPasswordChanged = vi.fn();
    render(<Step2AdminPassword status={tempStatus} onNext={onNext} onPasswordChanged={onPasswordChanged} />);
    fillAndSubmit('Strong#Pass1', 'Strong#Pass1');
    expect(await screen.findByText('不行')).toBeInTheDocument();
    expect(onPasswordChanged).not.toHaveBeenCalled();
    expect(onNext).not.toHaveBeenCalled();
  });
});
