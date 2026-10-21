import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
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
