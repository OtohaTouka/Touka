import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import LeftSidebar from '@/components/LeftSidebar';

// Mock dependencies to avoid rendering collapse error
vi.mock('@/lib/SystemStatusContext', () => ({
  useSystemStatus: () => ({ status: { mode: 'hybrid' } })
}));
vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({ sessions: [] })
}));
vi.mock('@/lib/providers/ChatProvider', () => ({
  useChat: () => ({
    activeSessionId: null,
    handleNewSession: vi.fn(),
    handleSwitchSession: vi.fn(),
    handleSetHome: vi.fn(),
    handleSetProject: vi.fn(),
    handleUnsetHome: vi.fn()
  })
}));
vi.mock('@/lib/providers/ProjectProvider', () => ({
  useProjects: () => ({ projects: [] })
}));
vi.mock('@/hooks/useSessionGroups', () => ({
  useSessionGroups: () => ({ sessionGroups: {} })
}));
vi.mock('@/hooks/useSessionActions', () => ({
  useSessionActions: () => ({
    editingSessionId: null,
    editTitle: '',
    deleteConfirmId: null,
    handleStartEdit: vi.fn(),
    handleSaveTitle: vi.fn(),
    setEditTitle: vi.fn(),
    handleDelete: vi.fn(),
    handleExport: vi.fn(),
    handleSplitExport: vi.fn()
  })
}));

vi.mock('@/components/sidebar/left/NavigationArea', () => ({
  NavigationArea: () => <div>Navigation Area</div>
}));

describe('LeftSidebar TDD-Red tests', () => {
  it('TDD-Red: does not read collapsed state from localStorage internally, only relies on isCollapsed prop', () => {
    // 當前 LeftSidebar 還在使用 useLocalStorage 內部維護狀態。
    // 在修復為 SSOT 後，LeftSidebar 應只根據 isCollapsed 渲染，內部不含 useLocalStorage。
    const { container } = render(<LeftSidebar isCollapsed={true} />);
    const outerDiv = container.firstChild as HTMLElement;
    expect(outerDiv?.classList.contains('w-0')).toBe(true);
  });
});

describe('LeftSidebar — 頂端的收合鈕（上導覽列拿掉後）', () => {
  it('有傳 onCollapse（桌面版）才有收合鈕，按了會呼叫', () => {
    const onCollapse = vi.fn();
    render(<LeftSidebar onCollapse={onCollapse} />);
    const button = screen.getByRole('button', { name: '收合側邊欄' });
    expect(button).toHaveAttribute('title', '收合側邊欄');
    fireEvent.click(button);
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });

  it('沒傳 onCollapse（手機版 Sheet）就沒有收合鈕', () => {
    render(<LeftSidebar />);
    expect(screen.queryByRole('button', { name: '收合側邊欄' })).toBeNull();
  });

  it('側欄剛展開、焦點掉在 body 時，焦點交給收合鈕', () => {
    const { rerender } = render(<LeftSidebar isCollapsed={true} onCollapse={vi.fn()} />);
    (document.activeElement as HTMLElement | null)?.blur();
    rerender(<LeftSidebar isCollapsed={false} onCollapse={vi.fn()} />);
    expect(screen.getByRole('button', { name: '收合側邊欄' })).toHaveFocus();
  });

  it('一開始就展開時不搶焦點', () => {
    (document.activeElement as HTMLElement | null)?.blur();
    render(<LeftSidebar isCollapsed={false} onCollapse={vi.fn()} />);
    expect(screen.getByRole('button', { name: '收合側邊欄' })).not.toHaveFocus();
  });
});
