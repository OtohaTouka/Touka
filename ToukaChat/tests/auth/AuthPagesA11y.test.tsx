/**
 * 登入／註冊頁無障礙行為測試（WCAG A1 批：3.3.2 label 關聯、1.3.5 autocomplete、2.4.6 標題）。
 * 渲染層驗證（非源碼掃描）：label 真的關聯到 input、a11y tree 有 level-1 heading。
 * mock 慣例沿用 AuthPages.test.tsx。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import LoginPage from '../../src/app/login/page';
import RegisterPage from '../../src/app/register/page';
import * as AuthContextModule from '../../src/contexts/AuthContext';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock('../../src/lib/api/authService', () => ({
  authService: { register: vi.fn() },
}));

function mockAuth() {
  vi.spyOn(AuthContextModule, 'useAuth').mockReturnValue({
    isAuthenticated: false,
    isLoading: false,
    accessToken: null,
    user: null,
    login: vi.fn(),
    logout: vi.fn(),
    refreshUser: vi.fn(),
  });
}

describe('Auth pages a11y (WCAG 3.3.2 / 1.3.5 / 2.4.6)', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockAuth();
  });

  it('Login：label 關聯 input、autocomplete 正確、有 level-1 heading', () => {
    render(<LoginPage />);
    const email = screen.getByLabelText(/email/i);
    const password = screen.getByLabelText(/password/i);
    expect(email).toHaveAttribute('autocomplete', 'username');
    expect(password).toHaveAttribute('autocomplete', 'current-password');
    expect(screen.getByRole('heading', { level: 1 })).toBeInTheDocument();
  });

  it('Register：label 關聯 input、新密碼 autocomplete、有 level-1 heading', () => {
    render(<RegisterPage />);
    const email = screen.getByLabelText(/email/i);
    const password = screen.getByLabelText(/password/i);
    expect(email).toHaveAttribute('autocomplete', 'email');
    expect(password).toHaveAttribute('autocomplete', 'new-password');
    expect(screen.getByRole('heading', { level: 1 })).toBeInTheDocument();
  });

  // A2 批（WCAG 2.1.1）：login↔register 切換連結須鍵盤可達（原為 <span onClick>）
  it('Login：切換到 Register 的連結是鍵盤可達的控制元件', () => {
    render(<LoginPage />);
    expect(screen.getByRole('button', { name: /register/i })).toBeInTheDocument();
  });

  it('Register：切換到 Login 的連結是鍵盤可達的控制元件', () => {
    render(<RegisterPage />);
    expect(screen.getByRole('button', { name: /login/i })).toBeInTheDocument();
  });
});
