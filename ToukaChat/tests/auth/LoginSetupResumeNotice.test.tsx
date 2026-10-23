/**
 * 登入頁的接續說明（ADR-0053 補記 2026-10-23）：精靈改完管理員密碼後，背景請求常在幾十秒內
 * 先把人帶到登入頁，「登入已逾期」提示會隨 Toaster 卸載消失；登入頁看到有效的接續點時要自己說明。
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import LoginPage from '@/app/login/page';
import * as AuthContextModule from '@/contexts/AuthContext';
import { saveSetupResume, SETUP_RESUME_TTL_MS } from '@/hooks/useSetupResume';
import { getSystemEpoch } from '@/lib/time_utils';

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));

const NOTICE = /管理員密碼已更新，請用新密碼登入/;

function renderLogin() {
  vi.spyOn(AuthContextModule, 'useAuth').mockReturnValue({
    isAuthenticated: false, isLoading: false, accessToken: null, user: null,
    login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
  });
  return render(<LoginPage />);
}

describe('登入頁的接續說明', () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.restoreAllMocks();
  });

  it('有有效的接續點：顯示說明，且只看不取', () => {
    saveSetupResume('admin-1', 'api_keys');
    renderLogin();
    expect(screen.getByRole('status')).toHaveTextContent(NOTICE);
    expect(window.localStorage.getItem('touka_setup_resume')).not.toBeNull();
  });

  it('沒有接續點：不顯示', () => {
    renderLogin();
    expect(screen.queryByText(NOTICE)).toBeNull();
  });

  it('接續點過期：不顯示', () => {
    window.localStorage.setItem('touka_setup_resume', JSON.stringify({
      userId: 'admin-1', stepId: 'api_keys', savedAt: getSystemEpoch() - SETUP_RESUME_TTL_MS - 1000,
    }));
    renderLogin();
    expect(screen.queryByText(NOTICE)).toBeNull();
  });
});
