import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { Step2AdminPassword } from '../steps/Step2AdminPassword';

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
    render(<Step2AdminPassword onNext={vi.fn()} />);
    expect(screen.getByText('密碼需求')).toBeInTheDocument();
    expect(screen.getByText('至少 8 個字元')).toBeInTheDocument();
  });

  it('rejects password without uppercase letter — F4 fix', () => {
    render(<Step2AdminPassword onNext={vi.fn()} />);
    fillAndSubmit('abc123!xyz', 'abc123!xyz');
    expect(screen.getByText(/密碼至少需一個大寫/)).toBeInTheDocument();
  });

  it('rejects password without lowercase letter — F4 fix', () => {
    render(<Step2AdminPassword onNext={vi.fn()} />);
    fillAndSubmit('ABC123!XYZ', 'ABC123!XYZ');
    expect(screen.getByText(/密碼至少需一個小寫/)).toBeInTheDocument();
  });

  it('rejects password without digit — F4 fix', () => {
    render(<Step2AdminPassword onNext={vi.fn()} />);
    fillAndSubmit('Abcdefgh!', 'Abcdefgh!');
    expect(screen.getByText(/密碼至少需一個數字/)).toBeInTheDocument();
  });

  it('rejects password without special character — F4 fix', () => {
    render(<Step2AdminPassword onNext={vi.fn()} />);
    fillAndSubmit('Abcdefg1', 'Abcdefg1');
    expect(screen.getByText(/密碼至少需一個特殊/)).toBeInTheDocument();
  });

  it('rejects password shorter than 8 characters — regression guard', () => {
    render(<Step2AdminPassword onNext={vi.fn()} />);
    fillAndSubmit('Ab1!', 'Ab1!');
    expect(screen.getByText(/密碼至少需 8/)).toBeInTheDocument();
  });

  it('passes validation for a strong password — no complexity error shown', () => {
    render(<Step2AdminPassword onNext={vi.fn()} />);
    fillAndSubmit('Strong#Pass1', 'Strong#Pass1');
    expect(screen.queryByText(/密碼至少/)).not.toBeInTheDocument();
  });
});
