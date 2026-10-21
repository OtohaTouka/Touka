import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// 兩位使用者：self=me（admin）、other（normal）。useAuth 回 me → 自我保護生效。
const users = [
  { id: 'me', email: 'me@touka.io', display_name: 'Me', avatar_url: '', bio: '', role: 'admin', status: 'active', owned_agents: [] },
  { id: 'other', email: 'other@touka.io', display_name: 'Other', avatar_url: '', bio: '', role: 'normal', status: 'active', owned_agents: [] },
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
  useSecuritySettings: () => ({ allAgents: [{ id: 'AgentX' }], fetchSecurityData: vi.fn() }),
}));
vi.mock('@/hooks/useConfirmToast', () => ({ useConfirmToast: () => ({ confirmToast: vi.fn() }) }));
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: { id: 'me' } }) }));

import { UsersTab } from '../UsersTab';

describe('UsersTab 自我保護（P1）', () => {
  it('自己那列不顯示刪除鈕（僅他人列有）', () => {
    render(<UsersTab />);
    // 兩位使用者、自己=me → 全表僅 1 個刪除鈕（other 的）
    expect(screen.getAllByTitle('刪除使用者')).toHaveLength(1);
  });

  it('編輯自己時 Role 下拉被停用（防自我降級）', () => {
    render(<UsersTab />);
    // me 是第一列；點其編輯鈕進入編輯模式
    fireEvent.click(screen.getAllByTitle('編輯資料')[0]);
    const roleSelect = screen.getByDisplayValue('管理員') as HTMLSelectElement;
    expect(roleSelect.disabled).toBe(true);
  });
});

describe('UsersTab a11y（P3）', () => {
  it('搜尋框有可及名稱（aria-label）', () => {
    render(<UsersTab />);
    expect(screen.getByLabelText('搜尋使用者 (Email, 名稱)...')).toBeTruthy();
  });

  it('UserForm 輸入有 label 關聯（htmlFor/id）', () => {
    render(<UsersTab />);
    fireEvent.click(screen.getAllByTitle('編輯資料')[0]); // 進編輯模式
    expect(screen.getByLabelText('電子郵件')).toBeTruthy();
    expect(screen.getByLabelText('顯示名稱')).toBeTruthy();
    expect(screen.getByLabelText('角色')).toBeTruthy();
  });

  it('指派代理人按鈕有 aria-pressed', () => {
    render(<UsersTab />);
    fireEvent.click(screen.getAllByTitle('編輯資料')[0]);
    const agentBtn = screen.getByRole('button', { name: 'AgentX' });
    expect(agentBtn.getAttribute('aria-pressed')).not.toBeNull();
  });
});
