import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { formatToTaipeiDate } from '@/lib/time_utils';
import { SessionTree } from '@/components/sidebar/left/SessionTree';
import type { AgentInfo, AgentsStatus } from '@/lib/providers/AgentProvider';
import type { SessionWorkStatus } from '@/lib/session_activity';
import type { Project, Session } from '@/lib/types';

const state = vi.hoisted(() => ({
  sessions: [] as Session[],
  agents: [] as AgentInfo[],
  agentsStatus: 'ready' as AgentsStatus,
  projects: [] as Project[],
  activeSessionId: null as string | null,
  statuses: {} as Record<string, SessionWorkStatus>,
  activityTimes: {} as Record<string, number>,
  userId: 'user-1' as string | undefined,
  handleSetHome: vi.fn(),
  handleUnsetHome: vi.fn(),
  handleStartEdit: vi.fn(),
  editingSessionId: null as string | null,
}));

vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: state.userId ? { id: state.userId } : null }) }));
vi.mock('@/lib/providers/SessionListProvider', () => ({ useSessionList: () => ({ sessions: state.sessions }) }));
vi.mock('@/lib/providers/AgentProvider', () => ({ useAgents: () => ({ agents: state.agents, agentsStatus: state.agentsStatus }) }));
vi.mock('@/lib/providers/ProjectProvider', () => ({ useProjects: () => ({ projects: state.projects }) }));
vi.mock('@/lib/providers/ChatProvider', () => ({
  useChat: () => ({
    activeSessionId: state.activeSessionId,
    handleSetHome: state.handleSetHome,
    handleUnsetHome: state.handleUnsetHome,
    handleSetProject: vi.fn(),
  }),
}));
vi.mock('@/lib/providers/SessionActivityProvider', () => ({
  useSessionActivity: () => ({
    statusOf: (id: string) => state.statuses[id] ?? null,
    activityTimeOf: (id: string) => state.activityTimes[id],
  }),
}));
vi.mock('@/hooks/useSessionActions', () => ({
  useSessionActions: () => ({
    editingSessionId: state.editingSessionId,
    editTitle: '新標題',
    deleteConfirmId: null,
    handleStartEdit: state.handleStartEdit,
    handleSaveTitle: vi.fn(),
    setEditTitle: vi.fn(),
    handleDelete: vi.fn(),
    clearDeleteConfirm: vi.fn(),
    handleExport: vi.fn(),
    handleSplitExport: vi.fn(),
  }),
}));

function session(id: string, agentId: string | null, updatedAt: number, extra: Partial<Session> = {}): Session {
  return { id, title: id, messages: [], updatedAt, agent_id: agentId, ...extra };
}

function soul(id: string, type = 'soul'): AgentInfo {
  return { id, name: id, type, is_group: false };
}

const onSwitch = vi.fn();
const renderTree = (activeMode = 'chat') => render(<SessionTree activeMode={activeMode} onSwitch={onSwitch} />);
const rows = () => screen.getAllByRole('treeitem').map(row => row.getAttribute('aria-label') ?? row.textContent);
const row = (name: string | RegExp) => screen.getByRole('treeitem', { name });
const arrowOf = (name: string | RegExp) => within(row(name)).getByTestId('session-tree-toggle');

beforeEach(() => {
  window.localStorage.clear();
  state.sessions = [];
  state.agents = [soul('Alice'), soul('Bob')];
  state.agentsStatus = 'ready';
  state.projects = [];
  state.activeSessionId = null;
  state.statuses = {};
  state.activityTimes = {};
  state.userId = 'user-1';
  state.editingSessionId = null;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('SessionTree — 結構', () => {
  it('資料夾是靈魂名稱、家對話的標題放 title；子對話縮一層，其他對話在下面', () => {
    state.sessions = [
      session('home', 'Alice', 3, { title: '我家', is_home: true }),
      session('child', 'Alice', 2),
      session('free', 'gpt-5', 1),
    ];
    renderTree();
    expect(rows()).toEqual(['Alice，家對話：我家', 'child', 'free，gpt-5']);
    const folder = row(/^Alice/);
    expect(folder).toHaveAttribute('aria-level', '1');
    expect(folder).toHaveAttribute('aria-expanded', 'true');
    expect(within(folder).getByTitle('我家')).toHaveTextContent('Alice');
    expect(row('child')).toHaveAttribute('aria-level', '2');
    const others = screen.getByRole('group', { name: '其他對話' });
    expect(within(others).getByRole('treeitem', { name: /^free/ })).toHaveAttribute('aria-level', '1');
  });

  it('清單的位置資訊正確', () => {
    state.sessions = [session('a1', 'Alice', 4), session('a2', 'Alice', 3), session('b1', 'Bob', 2)];
    renderTree();
    expect(row('Alice')).toHaveAttribute('aria-posinset', '1');
    expect(row('Alice')).toHaveAttribute('aria-setsize', '2');
    expect(row('a2')).toHaveAttribute('aria-posinset', '2');
    expect(row('a2')).toHaveAttribute('aria-setsize', '2');
    expect(row('Bob')).toHaveAttribute('aria-posinset', '2');
  });

  it('「還有 N 個」也算清單的一列', () => {
    state.sessions = [1, 2, 3, 4, 5, 6, 7].map(n => session(`a${n}`, 'Alice', n));
    renderTree();
    expect(row('a7')).toHaveAttribute('aria-setsize', '6');
    expect(row('還有 2 個')).toHaveAttribute('aria-posinset', '6');
    expect(row('還有 2 個')).toHaveAttribute('aria-setsize', '6');
  });

  it('列的名稱念出生成中與無痕', () => {
    state.statuses = { busy: { kind: 'replying' } };
    state.sessions = [session('busy', 'Alice', 2), session('secret', 'Alice', 1, { incognito: true })];
    renderTree();
    expect(rows()).toEqual(['Alice', 'busy，回覆中', 'secret，無痕對話']);
  });

  it('列的補充說明有日期與則數，報讀器念得到（舊版的按鈕念得到）', () => {
    state.sessions = [session('a1', 'Alice', 1, { round_count: 3 })];
    renderTree();
    expect(row('a1')).toHaveAccessibleDescription(/1970.*，3 則對話/);
    expect(within(row('a1')).getByText('3 則對話')).toBeVisible();
  });

  it('家的圖示在可以點的範圍裡', () => {
    state.sessions = [session('h', 'Alice', 2, { title: '我家', is_home: true }), session('a1', 'Alice', 1)];
    renderTree();
    expect(within(row(/^Alice/)).getByTitle('我家').querySelector('svg')).not.toBeNull();
  });

  it('只有家、底下沒有子對話：沒有箭頭，也不說自己能展開', () => {
    state.sessions = [session('h', 'Alice', 1, { title: '我家', is_home: true })];
    renderTree();
    expect(row(/^Alice/)).not.toHaveAttribute('aria-expanded');
    expect(within(row(/^Alice/)).queryByTestId('session-tree-toggle')).toBeNull();
    fireEvent.keyDown(row(/^Alice/), { key: 'ArrowLeft' });
    expect(window.localStorage.getItem('touka_session_tree_collapsed:user-1')).toBeNull();
  });

  it('其他對話標模型：群聊寫群聊、清單裡有的寫名稱、沒有 agent 的不標', () => {
    state.agents = [{ id: 'mine', name: '我的模型', type: 'custom', is_group: false }];
    state.sessions = [session('g', 'ToukaGroup', 3), session('c', 'mine', 2), session('n', null, 1)];
    renderTree();
    expect(rows()).toEqual(['g，群聊', 'c，我的模型', 'n']);
  });

  it('靈魂清單還沒讀到先畫骨架，不畫樹', () => {
    state.agentsStatus = 'loading';
    state.sessions = [session('a1', 'Alice', 1)];
    renderTree();
    expect(screen.queryByRole('tree')).toBeNull();
    expect(screen.getByRole('status', { name: '對話清單載入中' })).toHaveAttribute('aria-busy', 'true');
  });

  it('專案畫面不等靈魂清單', () => {
    state.agentsStatus = 'loading';
    state.sessions = [session('a1', 'Alice', 1)];
    renderTree('projects');
    expect(screen.getByRole('tree')).toBeInTheDocument();
  });

  it('清單讀不到時仍畫得出來，純 ID 放其他對話', () => {
    state.agentsStatus = 'failed';
    state.agents = [];
    state.sessions = [session('a1', 'Alice', 1)];
    renderTree();
    expect(rows()).toEqual(['a1，Alice']);
  });

  it('沒有對話時不畫空的 tree', () => {
    renderTree();
    expect(screen.queryByRole('tree')).toBeNull();
  });
});

describe('SessionTree — 資料夾那一列', () => {
  beforeEach(() => {
    state.sessions = [
      session('home', 'Alice', 3, { title: '我家', is_home: true }),
      session('child', 'Alice', 2),
      session('b1', 'Bob', 1),
    ];
  });

  it('點名字開家對話，不收合', () => {
    renderTree();
    fireEvent.click(within(row(/^Alice/)).getByText('Alice'));
    expect(onSwitch).toHaveBeenCalledWith('home');
    expect(row(/^Alice/)).toHaveAttribute('aria-expanded', 'true');
  });

  it('點箭頭只收合展開，不開對話', () => {
    renderTree();
    fireEvent.click(arrowOf(/^Alice/));
    expect(onSwitch).not.toHaveBeenCalled();
    expect(row(/^Alice/)).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('treeitem', { name: 'child' })).toBeNull();
    fireEvent.click(arrowOf(/^Alice/));
    expect(row('child')).toBeInTheDocument();
  });

  it('沒有家的靈魂點名字只展開：已經展開就不動，也不開對話', () => {
    renderTree();
    fireEvent.click(arrowOf('Bob'));
    expect(row('Bob')).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(within(row('Bob')).getByText('Bob'));
    expect(row('Bob')).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(within(row('Bob')).getByText('Bob'));
    expect(row('Bob')).toHaveAttribute('aria-expanded', 'true');
    expect(onSwitch).not.toHaveBeenCalled();
  });

  it('資料夾列的「⋯」是家對話的選單；沒有家就沒有', () => {
    renderTree();
    expect(within(row(/^Alice/)).getByRole('button', { name: '會話選項：我家' })).toBeInTheDocument();
    expect(within(row('Bob')).queryByRole('button')).toBeNull();
  });

  it('開著家對話時，資料夾列標 aria-current', () => {
    state.activeSessionId = 'home';
    renderTree();
    expect(row(/^Alice/)).toHaveAttribute('aria-current', 'true');
  });
});

describe('SessionTree — 開著的對話在收合的資料夾裡', () => {
  it('家資料夾：那一列標目前項目，Tab 站也在那裡；展開後回到對話那一列', () => {
    state.sessions = [session('b-new', 'Bob', 9), session('h', 'Alice', 2, { title: '我家', is_home: true }), session('a1', 'Alice', 1)];
    state.activeSessionId = 'a1';
    renderTree();
    fireEvent.click(arrowOf(/^Alice/));
    const folder = row(/^Alice/);
    expect(folder).toHaveAttribute('aria-current', 'true');
    // 開著的是子對話，不能只念成「家對話是目前項目」
    expect(folder).toHaveAccessibleName('Alice，家對話：我家，開著的對話在裡面');
    expect(screen.getAllByRole('treeitem').filter(item => item.tabIndex === 0)).toEqual([folder]);

    fireEvent.click(arrowOf(/^Alice/));
    expect(row(/^Alice/)).not.toHaveAttribute('aria-current');
    expect(row(/^Alice/)).toHaveAccessibleName('Alice，家對話：我家');
    expect(row('a1')).toHaveAttribute('aria-current', 'true');
  });

  it('沒有家的資料夾一樣', () => {
    state.sessions = [session('b-new', 'Bob', 9), session('a1', 'Alice', 1)];
    state.activeSessionId = 'a1';
    renderTree();
    fireEvent.click(arrowOf('Alice'));
    const folder = row(/^Alice/);
    expect(folder).toHaveAttribute('aria-current', 'true');
    expect(folder).toHaveAccessibleName('Alice，開著的對話在裡面');
    expect(screen.getAllByRole('treeitem').filter(item => item.tabIndex === 0)).toEqual([folder]);
  });

  it('家對話本身開著又沒有收合：名稱不多念', () => {
    state.sessions = [session('h', 'Alice', 2, { title: '我家', is_home: true }), session('a1', 'Alice', 1)];
    state.activeSessionId = 'h';
    renderTree();
    expect(row(/^Alice/)).toHaveAccessibleName('Alice，家對話：我家');
  });

  it('開著的不在這個資料夾：不標', () => {
    state.sessions = [session('a1', 'Alice', 2), session('b1', 'Bob', 1)];
    state.activeSessionId = 'b1';
    renderTree();
    fireEvent.click(arrowOf('Alice'));
    expect(row('Alice')).not.toHaveAttribute('aria-current');
  });

  it('專案分組一樣', () => {
    state.projects = [{ id: 'p1', name: 'Alpha', root_path: '/a', description: '', updatedAt: 1 }];
    state.sessions = [session('in', 'Alice', 1, { project_id: 'p1' })];
    state.activeSessionId = 'in';
    renderTree('projects');
    fireEvent.click(arrowOf(/Alpha/));
    expect(row(/Alpha/)).toHaveAttribute('aria-current', 'true');
  });
});

describe('SessionTree — 還有 N 個', () => {
  beforeEach(() => {
    state.sessions = Array.from({ length: 7 }, (_, index) => session(`s${index}`, 'Alice', 100 - index));
  });

  it('先列 5 筆，其餘收起來；按了全列', () => {
    renderTree();
    expect(rows()).toEqual(['Alice', 's0', 's1', 's2', 's3', 's4', '還有 2 個']);
    fireEvent.click(row('還有 2 個'));
    expect(rows()).toEqual(['Alice', 's0', 's1', 's2', 's3', 's4', 's5', 's6']);
  });

  it('開著的對話不收起來', () => {
    state.activeSessionId = 's6';
    renderTree();
    expect(rows()).toEqual(['Alice', 's0', 's1', 's2', 's3', 's4', 's6', '還有 1 個']);
  });

  it('鍵盤按「還有 N 個」，焦點交給同位置新出現的那一列', () => {
    renderTree();
    const more = row('還有 2 個');
    act(() => more.focus());
    fireEvent.keyDown(more, { key: 'Enter' });
    expect(row('s5')).toHaveFocus();
  });
});

describe('SessionTree — 收合狀態', () => {
  beforeEach(() => {
    state.sessions = [session('a1', 'Alice', 2), session('b1', 'Bob', 1)];
  });

  it('重掛後還在；換帳號不受影響，也沒有殘留', () => {
    const first = renderTree();
    fireEvent.click(arrowOf('Alice'));
    first.unmount();

    const again = renderTree();
    expect(row('Alice')).toHaveAttribute('aria-expanded', 'false');
    again.unmount();

    state.userId = 'user-2';
    const other = renderTree();
    expect(row('Alice')).toHaveAttribute('aria-expanded', 'true');
    other.unmount();

    state.userId = 'user-1';
    renderTree();
    expect(row('Alice')).toHaveAttribute('aria-expanded', 'false');
  });

  it('同一個元件換身分就重掛，不沿用上一個帳號的狀態', () => {
    const { rerender } = renderTree();
    fireEvent.click(arrowOf('Alice'));
    state.userId = 'user-2';
    rerender(<SessionTree activeMode="chat" onSwitch={onSwitch} />);
    expect(row('Alice')).toHaveAttribute('aria-expanded', 'true');
  });

  it('只有無痕對話的資料夾不寫進瀏覽器', () => {
    state.sessions = [session('secret', 'Alice', 1, { incognito: true })];
    renderTree();
    fireEvent.click(arrowOf('Alice'));
    expect(row('Alice')).toHaveAttribute('aria-expanded', 'false');
    expect(window.localStorage.getItem('touka_session_tree_collapsed:user-1')).toBeNull();
  });

  it('有一般對話的資料夾寫進瀏覽器，內容只有資料夾鍵', () => {
    state.sessions = [session('secret', 'Alice', 2, { incognito: true }), session('a1', 'Alice', 1)];
    renderTree();
    fireEvent.click(arrowOf('Alice'));
    expect(JSON.parse(window.localStorage.getItem('touka_session_tree_collapsed:user-1') ?? '{}')).toEqual({ alice: true });
  });
});

describe('SessionTree — 設為家', () => {
  const openMenu = (name: string | RegExp) => fireEvent.keyDown(row(name), { key: 'F10', shiftKey: true });

  it('只有靈魂的對話看得到', async () => {
    state.agents = [soul('Alice'), { id: 'mine', name: 'mine', type: 'custom', is_group: false }];
    state.sessions = [session('a1', 'Alice', 2), session('m1', 'mine', 1)];
    renderTree();
    openMenu('a1');
    expect(await screen.findByRole('menuitem', { name: '設為家對話' })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    cleanup();
    renderTree();
    openMenu(/^m1/);
    await screen.findByRole('menu');
    expect(screen.queryByRole('menuitem', { name: '設為家對話' })).toBeNull();
  });

  it('設為家要先確認，按先不要就不送', async () => {
    state.sessions = [session('a1', 'Alice', 2)];
    renderTree();
    openMenu('a1');
    fireEvent.click(await screen.findByRole('menuitem', { name: '設為家對話' }));
    const dialog = await screen.findByRole('dialog');
    expect(state.handleSetHome).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: '先不要' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(state.handleSetHome).not.toHaveBeenCalled();
    await waitFor(() => expect(document.activeElement?.getAttribute('role')).toBe('treeitem'));
  });

  it('確認之後才設家', async () => {
    state.sessions = [session('a1', 'Alice', 2)];
    renderTree();
    openMenu('a1');
    fireEvent.click(await screen.findByRole('menuitem', { name: '設為家對話' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: '設為家' }));
    expect(state.handleSetHome).toHaveBeenCalledWith('a1');
  });

  it('取消家也要先確認', async () => {
    state.sessions = [session('h', 'Alice', 1, { is_home: true })];
    renderTree();
    openMenu(/^Alice/);
    fireEvent.click(await screen.findByRole('menuitem', { name: '取消家對話' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: '取消家' }));
    expect(state.handleUnsetHome).toHaveBeenCalledWith('h');
  });

  it('舊家有標籤，也不能切分存入舊對話庫', async () => {
    state.sessions = [session('o', 'Alice', 1, { is_old_home: true })];
    renderTree();
    expect(row(/舊家/)).toBeInTheDocument();
    openMenu(/舊家/);
    expect(await screen.findByRole('menuitem', { name: '設為家對話' })).toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: /切分/ })).toBeNull();
  });

  it('家對話看得到取消', async () => {
    state.sessions = [session('h', 'Alice', 1, { is_home: true })];
    renderTree();
    openMenu(/^Alice/);
    expect(await screen.findByRole('menuitem', { name: '取消家對話' })).toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: '設為家對話' })).toBeNull();
  });
});

describe('SessionTree — 鍵盤', () => {
  beforeEach(() => {
    state.sessions = [
      session('h', 'Alice', 4, { title: '我家', is_home: true }),
      session('a1', 'Alice', 3),
      session('b1', 'Bob', 2),
      session('free', 'gpt-5', 1),
    ];
  });

  const tabbable = () => screen.getAllByRole('treeitem').filter(item => item.tabIndex === 0);

  it('只有一列在 Tab 順序；「⋯」都不在', () => {
    renderTree();
    expect(tabbable()).toHaveLength(1);
    expect(tabbable()[0]).toHaveAttribute('aria-label', 'Alice，家對話：我家');
    for (const button of screen.getAllByRole('button', { name: /^會話選項：/ })) expect(button).toHaveAttribute('tabindex', '-1');
  });

  it('Tab 站在開著的對話；家對話開著時在資料夾列', () => {
    state.activeSessionId = 'a1';
    const { unmount } = renderTree();
    expect(tabbable()[0]).toHaveAttribute('aria-label', 'a1');
    unmount();
    state.activeSessionId = 'h';
    renderTree();
    expect(tabbable()[0]).toHaveAttribute('aria-label', 'Alice，家對話：我家');
  });

  it('開著的家不在第一個資料夾時，Tab 站也在它那一列', () => {
    state.sessions = [session('b-new', 'Bob', 9), ...state.sessions];
    state.activeSessionId = 'h';
    renderTree();
    expect(tabbable()[0]).toHaveAttribute('aria-label', 'Alice，家對話：我家');
  });

  it('鍵盤叫出的 contextmenu（button 是 0）不跳瀏覽器選單；滑鼠右鍵照舊', () => {
    renderTree();
    expect(fireEvent.contextMenu(row('a1'), { button: 0 })).toBe(false);
    expect(fireEvent.contextMenu(row('a1'), { button: 2 })).toBe(true);
  });

  it('改名結束輸入框消失：焦點回到這一列', () => {
    state.editingSessionId = 'a1';
    const { rerender } = renderTree();
    act(() => screen.getByDisplayValue('新標題').focus());
    state.editingSessionId = null;
    rerender(<SessionTree activeMode="chat" onSwitch={onSwitch} />);
    expect(row('a1')).toHaveFocus();
  });

  it('改名結束時焦點已經在別處，就不搶', () => {
    const outside = document.createElement('button');
    document.body.appendChild(outside);
    try {
      state.editingSessionId = 'a1';
      const { rerender } = renderTree();
      act(() => outside.focus());
      state.editingSessionId = null;
      rerender(<SessionTree activeMode="chat" onSwitch={onSwitch} />);
      expect(outside).toHaveFocus();
    } finally {
      outside.remove();
    }
  });

  it('上下鍵跨資料夾與其他對話', () => {
    renderTree();
    const order = ['Alice，家對話：我家', 'a1', 'Bob', 'b1', 'free，gpt-5'];
    act(() => row(order[0]).focus());
    for (let index = 1; index < order.length; index += 1) {
      fireEvent.keyDown(document.activeElement as Element, { key: 'ArrowDown' });
      expect(document.activeElement).toHaveAttribute('aria-label', order[index]);
    }
  });

  it('左鍵收合、右鍵展開，收合狀態有存', () => {
    renderTree();
    act(() => row('Bob').focus());
    fireEvent.keyDown(row('Bob'), { key: 'ArrowLeft' });
    expect(row('Bob')).toHaveAttribute('aria-expanded', 'false');
    expect(JSON.parse(window.localStorage.getItem('touka_session_tree_collapsed:user-1') ?? '{}')).toEqual({ bob: true });
    fireEvent.keyDown(row('Bob'), { key: 'ArrowRight' });
    expect(row('Bob')).toHaveAttribute('aria-expanded', 'true');
  });

  it('Enter 開對話；資料夾列開家對話；沒有家的展開', () => {
    renderTree();
    fireEvent.keyDown(row('a1'), { key: 'Enter' });
    expect(onSwitch).toHaveBeenLastCalledWith('a1');
    fireEvent.keyDown(row(/^Alice/), { key: 'Enter' });
    expect(onSwitch).toHaveBeenLastCalledWith('h');
    fireEvent.click(arrowOf('Bob'));
    fireEvent.keyDown(row('Bob'), { key: 'Enter' });
    expect(row('Bob')).toHaveAttribute('aria-expanded', 'true');
    expect(onSwitch).toHaveBeenCalledTimes(2);
  });

  it.each([
    ['Shift+F10', { key: 'F10', shiftKey: true }],
    ['選單鍵', { key: 'ContextMenu' }],
  ])('%s 打開「⋯」，關掉後焦點回到這一列', async (_label, init) => {
    renderTree();
    const target = row('a1');
    act(() => target.focus());
    fireEvent.keyDown(target, init);
    const menu = await screen.findByRole('menu');
    expect(within(menu).getByRole('menuitem', { name: '重命名' })).toBeInTheDocument();
    fireEvent.keyDown(menu, { key: 'Escape' });
    await vi.waitFor(() => expect(row('a1')).toHaveFocus());
  });

  it('只按 F10 不開選單', () => {
    renderTree();
    fireEvent.keyDown(row('a1'), { key: 'F10' });
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('選單裡的方向鍵不移動清單的焦點', async () => {
    renderTree();
    const target = row('a1');
    act(() => target.focus());
    fireEvent.keyDown(target, { key: 'F10', shiftKey: true });
    const menu = await screen.findByRole('menu');
    fireEvent.keyDown(within(menu).getAllByRole('menuitem')[0], { key: 'ArrowDown' });
    // 選單開著時外面被標成 aria-hidden
    expect(screen.getByRole('treeitem', { name: 'a1', hidden: true }).tabIndex).toBe(0);
    fireEvent.keyDown(menu, { key: 'Escape' });
  });

  it('改名輸入框有名稱', () => {
    state.editingSessionId = 'a1';
    renderTree();
    expect(screen.getByRole('textbox', { name: '重命名' })).toHaveValue('新標題');
  });

  it('改名輸入框裡方向鍵不移動焦點，Shift+F10 不開選單', () => {
    state.editingSessionId = 'a1';
    renderTree();
    const input = screen.getByDisplayValue('新標題');
    act(() => input.focus());
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input).toHaveFocus();
    fireEvent.keyDown(input, { key: 'F10', shiftKey: true });
    expect(screen.queryByRole('menu')).toBeNull();
  });
});

describe('SessionTree — 工作狀態', () => {
  it('狀態寫進列的名稱；心跳顯示進度，小點與等您處理畫出來', () => {
    state.statuses = {
      working: { kind: 'heartbeat', current: 2, total: 5 },
      asking: { kind: 'awaiting' },
      done: { kind: 'unseen' },
    };
    state.sessions = [session('working', 'Alice', 3), session('asking', 'Alice', 2), session('done', 'Alice', 1)];
    renderTree();
    expect(within(row('working，心跳 2/5')).getByTestId('session-status-text')).toHaveTextContent('心跳 2/5');
    expect(within(row('asking，等您處理')).getByTestId('session-status-awaiting')).toBeInTheDocument();
    expect(within(row('done，有新內容')).getByTestId('session-status-unseen')).toBeInTheDocument();
  });

  it('工作中：名稱與手機的小點會呼吸；等您處理不動', () => {
    state.statuses = { working: { kind: 'replying' }, asking: { kind: 'awaiting' } };
    state.sessions = [session('working', 'Alice', 2), session('asking', 'Alice', 1)];
    renderTree();
    expect(within(row('working，回覆中')).getByTestId('session-name')).toHaveClass('md:animate-breathe');
    expect(within(row('working，回覆中')).getByTestId('session-status-working-dot')).toHaveClass('animate-breathe');
    expect(within(row('asking，等您處理')).getByTestId('session-name')).not.toHaveClass('md:animate-breathe');
    expect(within(row('asking，等您處理')).queryByTestId('session-status-working-dot')).toBeNull();
    expect(row('asking，等您處理').innerHTML).not.toContain('animate-');
  });

  it('工作中的不收進「還有 N 個」，等您處理也一樣；小點會收', () => {
    state.statuses = { a1: { kind: 'replying' }, a2: { kind: 'awaiting' }, a3: { kind: 'unseen' } };
    state.sessions = [1, 2, 3, 4, 5, 6, 7, 8, 9].map(n => session(`a${n}`, 'Alice', n));
    renderTree();
    expect(row('a1，回覆中')).toBeInTheDocument();
    expect(row('a2，等您處理')).toBeInTheDocument();
    expect(screen.queryByRole('treeitem', { name: /^a3/ })).toBeNull();
    expect(row('還有 2 個')).toBeInTheDocument();
  });

  it('資料夾收起來也會亮：最要緊的狀態畫在資料夾那一列', () => {
    state.statuses = { child: { kind: 'replying' }, other: { kind: 'awaiting' } };
    state.sessions = [session('child', 'Alice', 2), session('other', 'Alice', 1), session('bob-1', 'Bob', 1)];
    window.localStorage.setItem('touka_session_tree_collapsed:user-1', JSON.stringify({ alice: true }));
    renderTree();
    const folder = row('Alice，等您處理');
    expect(within(folder).getByTestId('session-status-awaiting')).toBeInTheDocument();
    expect(row('Bob')).toBeInTheDocument();
  });

  it('沒有家的資料夾收起來：名稱呼吸', () => {
    state.statuses = { child: { kind: 'replying' } };
    state.sessions = [session('child', 'Alice', 1)];
    window.localStorage.setItem('touka_session_tree_collapsed:user-1', JSON.stringify({ alice: true }));
    renderTree();
    expect(within(row('Alice，回覆中')).getByTestId('session-name')).toHaveClass('md:animate-breathe');
  });

  it('專案分組收起來也會亮', () => {
    state.projects = [{ id: 'p1', name: 'Alpha', root_path: '/a', description: '', updatedAt: 1 }];
    state.statuses = { in: { kind: 'awaiting' } };
    state.sessions = [session('in', 'Alice', 1, { project_id: 'p1' })];
    renderTree('projects');
    expect(screen.queryByRole('treeitem', { name: /Alpha.*等您處理/ })).toBeNull();
    fireEvent.click(arrowOf(/^專案: Alpha/));
    expect(row(/Alpha.*等您處理/)).toBeInTheDocument();
  });

  it('列上的日期用看到它在工作的時刻，不是存檔時間', () => {
    const worked = 1_790_000_000_000;
    state.activityTimes = { a: worked };
    state.sessions = [session('a', 'Alice', 1_700_000_000_000)];
    renderTree();
    expect(row('a')).toHaveTextContent(formatToTaipeiDate(worked));
    expect(row('a')).not.toHaveTextContent(formatToTaipeiDate(1_700_000_000_000));
  });

  it('有家的資料夾收起來：家那一列帶上子對話的狀態', () => {
    state.statuses = { child: { kind: 'heartbeat', current: 1, total: 3 } };
    state.sessions = [session('home', 'Alice', 1, { is_home: true }), session('child', 'Alice', 2)];
    window.localStorage.setItem('touka_session_tree_collapsed:user-1', JSON.stringify({ alice: true }));
    renderTree();
    expect(row('Alice，家對話：home，心跳 1/3')).toBeInTheDocument();
  });

  it('資料夾展開時，看得到的子對話不重複算到資料夾列', () => {
    state.statuses = { child: { kind: 'replying' } };
    state.sessions = [session('child', 'Alice', 2)];
    renderTree();
    expect(row('Alice')).toBeInTheDocument();
    expect(row('child，回覆中')).toBeInTheDocument();
  });

  it('收在「還有 N 個」裡的小點算到資料夾列', () => {
    state.statuses = { a1: { kind: 'unseen' } };
    state.sessions = [1, 2, 3, 4, 5, 6].map(n => session(`a${n}`, 'Alice', n));
    renderTree();
    expect(row('Alice，有新內容')).toBeInTheDocument();
  });

  it('活動時間決定順序與列上的日期；存檔內容不動', () => {
    state.activityTimes = { old: 5_000_000 };
    state.sessions = [session('new', 'Alice', 2_000_000), session('old', 'Bob', 1_000_000)];
    renderTree();
    expect(rows()).toEqual(['Bob', 'old', 'Alice', 'new']);
    expect(state.sessions[1].updatedAt).toBe(1_000_000);
  });
});

describe('SessionTree — 專案畫面', () => {
  beforeEach(() => {
    state.projects = [{ id: 'p1', name: 'Alpha', root_path: '/a', description: '', updatedAt: 1 }];
    state.sessions = [session('in', 'Alice', 2, { project_id: 'p1' }), session('out', 'Alice', 1)];
  });

  it('維持依專案分組，同樣只算一站，不寫瀏覽器', () => {
    renderTree('projects');
    expect(rows()).toEqual(['專案: Alpha，1', 'in', '其他對話，1', 'out']);
    expect(screen.getAllByRole('treeitem').filter(item => item.tabIndex === 0)).toHaveLength(1);
    fireEvent.click(arrowOf(/^專案: Alpha/));
    expect(screen.queryByRole('treeitem', { name: 'in' })).toBeNull();
    expect(window.localStorage.getItem('touka_session_tree_collapsed:user-1')).toBeNull();
  });

  it('點專案名稱或按 Enter 都會收合展開，也顯示筆數（和改版前一樣）', () => {
    renderTree('projects');
    const name = /Alpha/;
    expect(row(name)).toHaveTextContent('1');
    expect(row(name)).toHaveAccessibleName(/Alpha，1$/);
    fireEvent.click(within(row(name)).getByText(/Alpha/));
    expect(row(name)).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(within(row(name)).getByText(/Alpha/));
    expect(row(name)).toHaveAttribute('aria-expanded', 'true');
    fireEvent.keyDown(row(name), { key: 'Enter' });
    expect(row(name)).toHaveAttribute('aria-expanded', 'false');
    expect(onSwitch).not.toHaveBeenCalled();
  });

  it('點對話走 onSwitch', () => {
    renderTree('projects');
    fireEvent.click(within(row('in')).getByText('in'));
    expect(onSwitch).toHaveBeenCalledWith('in');
  });
});
