/** Step6CreateUser a11y（#4 批次 4）：email/password label 與 input 正確關聯（htmlFor/id）。 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import { Step6CreateUser } from '../steps/Step6CreateUser';

vi.mock('@/lib/connections', () => ({
  getActiveProxyUrl: vi.fn(() => 'http://localhost:8000'),
  fetchTouka: vi.fn(),
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn() }));

describe('Step6CreateUser a11y', () => {
  it('email 與 password label 與 input 正確關聯', () => {
    render(<Step6CreateUser onNext={vi.fn()} />);
    expect(screen.getByLabelText('電子郵件')).toBeInTheDocument();
    expect(screen.getByLabelText('密碼')).toBeInTheDocument();
  });
});
