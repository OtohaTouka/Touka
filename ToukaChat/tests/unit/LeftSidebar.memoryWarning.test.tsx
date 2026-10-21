import { afterEach, describe, it, expect, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import React from 'react';
import LeftSidebar from '@/components/LeftSidebar';

const state = vi.hoisted(() => ({ notices: undefined as unknown, systemNotices: undefined as unknown }));
vi.mock('@/lib/SystemStatusContext', () => ({
  useSystemStatus: () => ({
    status: { mode: 'hybrid', checkState: 'ready', memoryNotices: state.notices, systemNotices: state.systemNotices },
  }),
}));
vi.mock('@/lib/providers/SessionListProvider', () => ({ useSessionList: () => ({ sessions: [] }) }));
vi.mock('@/lib/providers/ChatProvider', () => ({
  useChat: () => ({
    activeSessionId: null,
    handleNewSession: vi.fn(),
    handleSwitchSession: vi.fn(),
    handleSetHome: vi.fn(),
    handleSetProject: vi.fn(),
    handleUnsetHome: vi.fn(),
  }),
}));
vi.mock('@/lib/providers/ProjectProvider', () => ({ useProjects: () => ({ projects: [] }) }));
vi.mock('@/hooks/useSessionGroups', () => ({ useSessionGroups: () => ({ sessionGroups: {} }) }));
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
    handleSplitExport: vi.fn(),
  }),
}));
vi.mock('@/components/sidebar/left/NavigationArea', () => ({ NavigationArea: () => <div>Navigation Area</div> }));

const marker = () => screen.queryByRole('img', { name: /記憶系統有警告/ });
// 測試順序是亂的（vitest sequence.shuffle）：系統提示的案例不能留給記憶提示的案例
afterEach(() => { cleanup(); state.systemNotices = undefined; });

describe('LeftSidebar memory warning marker', () => {
  it('marks the identity when a memory notice is a warning', () => {
    state.notices = [{ code: 'unmigrated_legacy_facts', level: 'info', count: 1 }, { code: 'future_code', level: 'warning' }];
    render(<LeftSidebar />);
    expect(marker()).not.toBeNull();
  });
  it('shows no marker for information-only notices', () => {
    state.notices = [{ code: 'unmigrated_legacy_facts', level: 'info', count: 1 }];
    render(<LeftSidebar />);
    expect(marker()).toBeNull();
  });
  it('shows no marker without notices', () => {
    state.notices = undefined;
    render(<LeftSidebar />);
    expect(marker()).toBeNull();
  });
});

// iso-c2 U2h：/health 的系統提示（設定讀不到、功能被停掉）也要在左側標出，右側欄收起時才看得到
describe('LeftSidebar system warning marker', () => {
  const systemMarker = () => screen.queryByRole('img', { name: /功能因設定問題停用/ });
  it('marks the identity when a system notice is a warning', () => {
    state.notices = undefined;
    state.systemNotices = [{ code: 'auth_policies_unreadable', level: 'warning' }];
    render(<LeftSidebar />);
    expect(systemMarker()).not.toBeNull();
  });
  it('shows no system marker without system notices', () => {
    state.systemNotices = undefined;
    render(<LeftSidebar />);
    expect(systemMarker()).toBeNull();
  });
});
