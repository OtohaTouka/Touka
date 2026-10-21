import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

const h = vi.hoisted(() => ({ createUser: vi.fn().mockResolvedValue(true) }));

vi.mock('@/hooks/settings/useUserSettings', () => ({
  useUserSettings: () => ({
    users: [],
    isFetchingUsers: false,
    isSavingUser: false,
    fetchUsers: vi.fn(),
    createUser: h.createUser,
    updateUser: vi.fn(),
    deleteUser: vi.fn(),
  }),
}));
vi.mock('@/hooks/settings/useSecuritySettings', () => ({
  useSecuritySettings: () => ({ allAgents: [], fetchSecurityData: vi.fn() }),
}));
vi.mock('@/hooks/useConfirmToast', () => ({ useConfirmToast: () => ({ confirmToast: vi.fn() }) }));
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: { id: 'me' } }) }));

import { UsersTab } from '../UsersTab';

function openCreateForm() {
  render(<UsersTab />);
  fireEvent.click(screen.getByRole('button', { name: /新增使用者/ }));
}

describe('UsersTab A5：建立表單驗證', () => {
  beforeEach(() => h.createUser.mockClear());

  it('email 空白 → 報錯且不送出', () => {
    openCreateForm();
    fireEvent.click(screen.getByRole('button', { name: /儲存變更/ }));
    expect(screen.getByText('請輸入電子郵件')).toBeTruthy();
    expect(h.createUser).not.toHaveBeenCalled();
  });

  it('email 格式錯誤 → 報錯且不送出', () => {
    openCreateForm();
    fireEvent.change(screen.getByLabelText('電子郵件'), { target: { value: 'bad' } });
    fireEvent.change(screen.getByLabelText('密碼'), { target: { value: 'password123' } });
    fireEvent.click(screen.getByRole('button', { name: /儲存變更/ }));
    expect(screen.getByText('電子郵件格式不正確')).toBeTruthy();
    expect(h.createUser).not.toHaveBeenCalled();
  });

  it('建立時密碼太短（<8）→ 報錯且不送出', () => {
    openCreateForm();
    fireEvent.change(screen.getByLabelText('電子郵件'), { target: { value: 'good@touka.io' } });
    fireEvent.change(screen.getByLabelText('密碼'), { target: { value: 'short' } });
    fireEvent.click(screen.getByRole('button', { name: /儲存變更/ }));
    expect(screen.getByText('密碼至少需 8 個字元')).toBeTruthy();
    expect(h.createUser).not.toHaveBeenCalled();
  });

  it('全部有效 → 呼叫 createUser', () => {
    openCreateForm();
    fireEvent.change(screen.getByLabelText('電子郵件'), { target: { value: 'good@touka.io' } });
    fireEvent.change(screen.getByLabelText('密碼'), { target: { value: 'password123' } });
    fireEvent.click(screen.getByRole('button', { name: /儲存變更/ }));
    expect(h.createUser).toHaveBeenCalled();
  });
});
