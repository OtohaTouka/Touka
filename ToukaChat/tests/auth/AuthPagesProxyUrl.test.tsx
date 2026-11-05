/**
 * 登入與註冊頁顯示目前連到哪個網址：打密碼前看得到密碼會送去哪，換完後端也看得到結果
 * （登出時整棵樹含 Toaster 卸載，提示框不會留下來，要說明就放這裡）。
 */
import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import LoginPage from '@/app/login/page';
import RegisterPage from '@/app/register/page';
import * as AuthContextModule from '@/contexts/AuthContext';

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));

const A = 'http://a.test:8000';
const B = 'http://b.test:9000';
const useList = (url: string) => window.localStorage.setItem('touka_connections',
  JSON.stringify([{ id: 'proxy', name: 'proxy', url, provider: 'proxy', protocol: 'openai', enabled: true }]));

describe.each([
  ['登入頁', LoginPage],
  ['註冊頁', RegisterPage],
])('%s顯示目前的網址', (_name, Page) => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.restoreAllMocks();
    vi.spyOn(AuthContextModule, 'useAuth').mockReturnValue({
      isAuthenticated: false, isLoading: false, accessToken: null, user: null,
      login: vi.fn(), logout: vi.fn(), refreshUser: vi.fn(),
    });
  });

  it('顯示清單裡目前的 Proxy 網址', () => {
    useList(`${A}/v1/`);
    render(<Page />);
    expect(screen.getByText(`連線到：${A}`)).toBeInTheDocument();
  });

  it('別的分頁改了網址時跟著變', () => {
    useList(A);
    render(<Page />);
    act(() => {
      useList(B);
      window.dispatchEvent(new StorageEvent('storage', { key: 'touka_connections' }));
    });
    expect(screen.getByText(`連線到：${B}`)).toBeInTheDocument();
  });

  it('空字串（沒有另外設定）顯示前端來源', () => {
    render(<Page />);
    expect(screen.getByText(`連線到：${window.location.origin}`)).toBeInTheDocument();
  });
});
