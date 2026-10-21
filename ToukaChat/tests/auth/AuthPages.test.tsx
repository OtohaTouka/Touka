import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, fireEvent } from '@testing-library/react';
import LoginPage from '../../src/app/login/page';
import RegisterPage from '../../src/app/register/page';
import * as AuthContextModule from '../../src/contexts/AuthContext';
import { authService, AuthRequestError } from '../../src/lib/api/authService';
import { errorMsg } from '../../src/lib/error_handler';

const mockPush = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush }),
}));

vi.mock('../../src/lib/api/authService', async importOriginal => ({
  ...await importOriginal<typeof import('../../src/lib/api/authService')>(),
  authService: {
    register: vi.fn(),
  }
}));

describe('Auth Pages UI', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it.each(['AUTH_INVALID_CREDENTIALS', 'AUTH_PERMISSION_DENIED', 'AUTH_RATE_LIMITED'] as const)(
    'renders the service-classified %s safely without redirecting', async code => {
      // authService.test covers HTTP classification; this seam verifies its real domain error in the real page.
      const login = vi.fn().mockRejectedValue(new AuthRequestError(code));
      const logger = vi.spyOn(console, 'error').mockImplementation(() => {});
      vi.spyOn(AuthContextModule, 'useAuth').mockReturnValue({
        isAuthenticated: false, isLoading: false, accessToken: null, user: null,
        login, logout: vi.fn(), refreshUser: vi.fn(),
      });
      render(<LoginPage />);
      fireEvent.change(screen.getByPlaceholderText(/email/i), { target: { value: 'test@test.com' } });
      fireEvent.change(screen.getByPlaceholderText(/password/i), { target: { value: 'private-password-sentinel' } });
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: /login/i })); });
      expect(await screen.findByText(errorMsg(code))).toBeInTheDocument();
      expect(screen.queryByText('private-password-sentinel')).not.toBeInTheDocument();
      expect(JSON.stringify(logger.mock.calls)).not.toContain('private-password-sentinel');
      expect(login).toHaveBeenCalledTimes(1);
      expect(mockPush).not.toHaveBeenCalled();
      logger.mockRestore();
    },
  );

  it('Login page should call login context method on submit', async () => {
    const mockLogin = vi.fn().mockResolvedValue(undefined);
    vi.spyOn(AuthContextModule, 'useAuth').mockReturnValue({
      isAuthenticated: false,
      isLoading: false,
      accessToken: null,
      user: null,
      login: mockLogin,
      logout: vi.fn(),
      refreshUser: vi.fn(),
    });

    render(<LoginPage />);
    
    const emailInput = screen.getByPlaceholderText(/email/i);
    const passwordInput = screen.getByPlaceholderText(/password/i);
    const submitButton = screen.getByRole('button', { name: /login/i });

    fireEvent.change(emailInput, { target: { value: 'test@test.com' } });
    fireEvent.change(passwordInput, { target: { value: 'password123' } });
    
    await act(async () => {
      fireEvent.click(submitButton);
    });

    expect(mockLogin).toHaveBeenCalledWith('test@test.com', 'password123');
    expect(mockPush).toHaveBeenCalledWith('/');
  });

  it('Login page should show error message on failure', async () => {
    const mockLogin = vi.fn().mockImplementation(() => Promise.reject(new Error('private-sentinel')));
    const logger = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(AuthContextModule, 'useAuth').mockReturnValue({
      isAuthenticated: false,
      isLoading: false,
      accessToken: null,
      user: null,
      login: mockLogin,
      logout: vi.fn(),
      refreshUser: vi.fn(),
    });

    await act(async () => {
      render(<LoginPage />);
    });
    const submitButton = screen.getByRole('button', { name: /login/i });

    await act(async () => {
      fireEvent.change(screen.getByPlaceholderText(/email/i), { target: { value: 'test@test.com' } });
      fireEvent.change(screen.getByPlaceholderText(/password/i), { target: { value: 'password123' } });
      fireEvent.click(submitButton);
    });

    expect(await screen.findByText(errorMsg('SYSTEM_ERROR'))).toBeInTheDocument();
    expect(screen.queryByText('private-sentinel')).not.toBeInTheDocument();
    expect(JSON.stringify(logger.mock.calls)).not.toContain('private-sentinel');
    logger.mockRestore();
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('keeps one login submission pending', async () => {
    let finish!: () => void;
    const login = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    vi.spyOn(AuthContextModule, 'useAuth').mockReturnValue({
      isAuthenticated: false, isLoading: false, accessToken: null, user: null,
      login, logout: vi.fn(), refreshUser: vi.fn(),
    });
    render(<LoginPage />);
    fireEvent.change(screen.getByPlaceholderText(/email/i), { target: { value: 'test@test.com' } });
    fireEvent.change(screen.getByPlaceholderText(/password/i), { target: { value: 'synthetic-password' } });
    const button = screen.getByRole('button', { name: /login/i });
    fireEvent.click(button); fireEvent.click(button);
    expect(login).toHaveBeenCalledTimes(1);
    await act(async () => { finish(); });
  });

  it('Register page should call authService.register and redirect', async () => {
    vi.mocked(authService.register).mockResolvedValueOnce({ id: '123' });
    
    render(<RegisterPage />);
    
    const emailInput = screen.getByPlaceholderText(/email/i);
    const passwordInput = screen.getByPlaceholderText(/password/i);
    const submitButton = screen.getByRole('button', { name: /register/i });

    fireEvent.change(emailInput, { target: { value: 'new@test.com' } });
    fireEvent.change(passwordInput, { target: { value: 'password123' } });
    
    await act(async () => {
      fireEvent.click(submitButton);
    });

    expect(authService.register).toHaveBeenCalledWith('new@test.com', 'password123');
    expect(mockPush).toHaveBeenCalledWith('/login');
  });
});
