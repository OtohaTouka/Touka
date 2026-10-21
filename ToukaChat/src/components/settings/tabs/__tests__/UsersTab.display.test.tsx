import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// 兩位使用者：u2 的 last_active_at 為 null（從未登入）。
const users = [
  { id: 'u1', email: 'a@touka.io', display_name: 'A', avatar_url: '', bio: '', role: 'admin', status: 'active', owned_agents: [], created_at: 1700000000, last_active_at: 1700500000 },
  { id: 'u2', email: 'b@touka.io', display_name: 'B', avatar_url: '', bio: '', role: 'normal', status: 'active', owned_agents: [], created_at: 1700000000, last_active_at: null },
];

vi.mock('@/hooks/settings/useUserSettings', () => ({
  useUserSettings: () => ({
    users,
    isFetchingUsers: false,
    isSavingUser: false,
    fetchUsers: vi.fn(),
    createUser: vi.fn(),
    updateUser: vi.fn(),
    deleteUser: vi.fn(),
  }),
}));
vi.mock('@/hooks/settings/useSecuritySettings', () => ({
  useSecuritySettings: () => ({ allAgents: [], fetchSecurityData: vi.fn() }),
}));
vi.mock('@/hooks/useConfirmToast', () => ({ useConfirmToast: () => ({ confirmToast: vi.fn() }) }));
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: { id: 'u1' } }) }));

import { UsersTab } from '../UsersTab';

describe('UsersTab A3：角色圖例', () => {
  it('顯示 admin 與 normal 角色說明', () => {
    render(<UsersTab />);
    expect(screen.getByText('管理員：可管理使用者、群組與系統設定。')).toBeTruthy();
    expect(screen.getByText('一般使用者：可使用對話與個人設定，無管理權限。')).toBeTruthy();
  });
});

describe('UsersTab A4：時間戳顯示', () => {
  it('每列顯示「建立於」，null 的 last_active 顯示「從未」', () => {
    render(<UsersTab />);
    expect(screen.getAllByText(/建立於/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/從未/)).toBeTruthy();
  });
});
