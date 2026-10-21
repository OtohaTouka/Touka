/**
 * M-fix: the workspace-root setting is admin-only (POST /v1/ui/settings/workspace is
 * gated by _require_admin_role). A non-admin previously saw an editable field they
 * could never save (generic CONFIG_SAVE_FAILED). The whole section is now hidden for
 * non-admins.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import WorkspaceSettings from '../WorkspaceSettings';

vi.mock('@/contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('@/hooks/useProjects', () => ({
  useProjects: () => ({
    fetchWorkspaceRoot: vi.fn().mockResolvedValue(''),
    saveWorkspaceRoot: vi.fn().mockResolvedValue(true),
  }),
}));

import { useAuth } from '@/contexts/AuthContext';

function authAs(role: string) {
  vi.mocked(useAuth).mockReturnValue({
    user: { role, email: 'u@test.com', id: '1', status: 'active' },
    isAuthenticated: true,
    isLoading: false,
    accessToken: null,
    login: vi.fn(),
    logout: vi.fn(),
    refreshUser: vi.fn(),
  });
}

describe('WorkspaceSettings（M-fix：工作區根目錄僅 admin 可見）', () => {
  it('非 admin：整個工作區設定隱藏（無輸入框）', () => {
    authAs('normal');
    const { container } = render(<WorkspaceSettings />);
    expect(container.firstChild).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('admin：渲染工作區根目錄輸入框', () => {
    authAs('admin');
    render(<WorkspaceSettings />);
    expect(screen.getByRole('textbox')).toBeInTheDocument();
  });
});
