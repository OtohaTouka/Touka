import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AuthGuard } from '../../src/components/AuthGuard';
import * as AuthContextModule from '../../src/contexts/AuthContext';

// Mock Next.js router
const mockPush = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({
    push: mockPush,
  }),
  usePathname: () => '/protected-route'
}));

describe('AuthGuard', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('should render children if authenticated', () => {
    vi.spyOn(AuthContextModule, 'useAuth').mockReturnValue({
      isAuthenticated: true,
      isLoading: false,
      accessToken: 'valid-token',
      user: null,
      login: vi.fn(),
      logout: vi.fn(),
      refreshUser: vi.fn(),
    });

    render(
      <AuthGuard>
        <div data-testid="protected-content">Protected Content</div>
      </AuthGuard>
    );

    expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('should render loading state if isLoading is true', () => {
    vi.spyOn(AuthContextModule, 'useAuth').mockReturnValue({
      isAuthenticated: false,
      isLoading: true,
      accessToken: null,
      user: null,
      login: vi.fn(),
      logout: vi.fn(),
      refreshUser: vi.fn(),
    });

    render(
      <AuthGuard>
        <div data-testid="protected-content">Protected Content</div>
      </AuthGuard>
    );

    expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    // Based on our implementation plan, it should show a loading skeleton or indicator
    expect(screen.getByTestId('auth-loading')).toBeInTheDocument();
    expect(mockPush).not.toHaveBeenCalled(); // Wait for loading to finish
  });

  it('should redirect to login if not authenticated', () => {
    vi.spyOn(AuthContextModule, 'useAuth').mockReturnValue({
      isAuthenticated: false,
      isLoading: false,
      accessToken: null,
      user: null,
      login: vi.fn(),
      logout: vi.fn(),
      refreshUser: vi.fn(),
    });

    render(
      <AuthGuard>
        <div data-testid="protected-content">Protected Content</div>
      </AuthGuard>
    );

    expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    expect(mockPush).toHaveBeenCalledWith('/login');
  });
});
