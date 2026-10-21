import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { toast } from 'sonner';
import { useSessionActions } from '../useSessionActions';
import { SessionItem } from '@/components/sidebar/left/SessionItem';

const mocks = vi.hoisted(() => ({ deleteSession: vi.fn(), updateSessionTitle: vi.fn(), updateMessages: vi.fn(), fetchTouka: vi.fn(), epoch: 1 }));
vi.mock('@/lib/providers/SessionListProvider', () => ({
  useSessionList: () => ({
    deleteSession: mocks.deleteSession, updateSessionTitle: mocks.updateSessionTitle,
    sessions: [{ id: 'a' }, { id: 'b' }, { id: 'home', is_home: true }],
  }),
}));
vi.mock('@/lib/providers/ChatProvider', () => ({ useChat: () => ({ handleUpdateSessionMessages: mocks.updateMessages }) }));
vi.mock('@/lib/providers/ModelProvider', () => ({ useModels: () => ({ selectedModel: 'synthetic-model' }) }));
vi.mock('@/lib/connections', () => ({ getActiveProxyUrl: () => 'http://synthetic', fetchTouka: mocks.fetchTouka,
  captureRequestScope: () => ({ proxyOrigin: 'http://synthetic', authenticatedPrincipalId: 'a', authEpoch: mocks.epoch }),
  isRequestScopeCurrent: (scope: { authEpoch: number }) => scope?.authEpoch === mocks.epoch,
}));
vi.mock('@/lib/error_handler', () => ({ logSSOTError: vi.fn(), getErrorMessage: vi.fn(), errorMsg: (code: string) => code }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), warning: vi.fn(), info: vi.fn(), success: vi.fn() } }));

const longSession = () => ({ id: 'a', title: 'a', updatedAt: 1, messages:
  Array.from({ length: 64 }, (_, index) => ({ id: `n${index}`,
    role: index % 2 ? 'assistant' as const : 'user' as const,
    content: `synthetic ${index}`, parentId: null, childrenIds: [] })) });

describe('刪除確認等待與失敗狀態', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.useFakeTimers(); mocks.epoch = 1; });
  afterEach(() => vi.useRealTimers());

  it.each(['failed', 'unknown'])('拆分保存 %s 不顯示完成', async status => {
    mocks.fetchTouka.mockResolvedValue({ ok: true });
    mocks.updateMessages.mockResolvedValue({ status, error_code: 1302 });
    const { result } = renderHook(() => useSessionActions());
    const messages = Array.from({ length: 64 }, (_, index) => ({
      id: `n${index}`, role: index % 2 ? 'assistant' as const : 'user' as const,
      content: `synthetic ${index}`, parentId: null, childrenIds: [],
    }));
    await act(async () => result.current.handleSplitExport({ id: 'a', title: 'a', messages, updatedAt: 1 }));
    expect(mocks.updateMessages).toHaveBeenCalledOnce();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('無痕拆分不傳 HTTP、不縮減完整記憶體樹', async () => {
    mocks.fetchTouka.mockResolvedValue({ ok: true });
    mocks.updateMessages.mockResolvedValue({ status: 'skipped', reason: 'incognito' });
    const session = { ...longSession(), incognito: true };
    const before = structuredClone(session);
    const { result } = renderHook(() => useSessionActions());
    await act(async () => result.current.handleSplitExport(session));
    expect(mocks.fetchTouka).not.toHaveBeenCalled();
    expect(mocks.updateMessages).not.toHaveBeenCalled();
    expect(session).toEqual(before);
    expect(toast.info).toHaveBeenCalledOnce();
  });

  it.each(['before_send', 'after_export'])('scope 改變 %s 不套用舊拆分，也不顯示舊成功', async when => {
    let finish!: (value: unknown) => void;
    mocks.fetchTouka.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    mocks.updateMessages.mockResolvedValue({ status: 'saved', representation: 'full', revision: 'r1' });
    const { result } = renderHook(() => useSessionActions());
    const split = result.current.handleSplitExport;
    if (when === 'before_send') mocks.epoch++;
    let pending!: Promise<void>;
    act(() => { pending = split(longSession()); });
    if (when === 'after_export') {
      expect(mocks.fetchTouka).toHaveBeenCalledOnce();
      mocks.epoch++;
    }
    await act(async () => { finish?.({ ok: true }); await pending; });
    if (when === 'before_send') expect(mocks.fetchTouka).not.toHaveBeenCalled();
    expect(mocks.updateMessages).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('改名等待保存，未知結果保留編輯器與內容', async () => {
    let finish!: (value: unknown) => void;
    mocks.updateSessionTitle.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const { result } = renderHook(() => useSessionActions());
    act(() => result.current.handleStartEdit({ id: 'a', title: 'draft', messages: [], updatedAt: 1 }));
    act(() => { result.current.handleSaveTitle('a'); result.current.handleSaveTitle('a'); });
    expect(result.current.editingSessionId).toBe('a');
    expect(mocks.updateSessionTitle).toHaveBeenCalledTimes(1);
    await act(async () => finish({ status: 'unknown', error_code: 1302 }));
    expect(result.current.editingSessionId).toBe('a');
    expect(result.current.editTitle).toBe('draft');
  });

  it('pending 不清 confirmation，雙按只送一次，失敗與原 timer 不隱藏確認', async () => {
    let finish!: (result: boolean) => void;
    mocks.deleteSession.mockReturnValue(new Promise<boolean>(resolve => { finish = resolve; }));
    const { result } = renderHook(() => useSessionActions());
    act(() => { result.current.handleDelete('a'); });
    act(() => { result.current.handleDelete('a'); result.current.handleDelete('a'); });
    expect(mocks.deleteSession).toHaveBeenCalledTimes(1);
    expect(result.current.deleteConfirmId).toBe('a');
    act(() => { vi.advanceTimersByTime(4000); });
    expect(result.current.deleteConfirmId).toBe('a');
    await act(async () => { finish(false); });
    expect(result.current.deleteConfirmId).toBe('a');
  });

  it('成功才清除；使用者主動關閉仍可清除閒置確認', async () => {
    mocks.deleteSession.mockResolvedValue(true);
    const { result } = renderHook(() => useSessionActions());
    act(() => { result.current.handleDelete('a'); });
    await act(async () => { await result.current.handleDelete('a'); });
    expect(result.current.deleteConfirmId).toBeNull();
    act(() => { result.current.handleDelete('b'); });
    act(() => { result.current.clearDeleteConfirm(); });
    expect(result.current.deleteConfirmId).toBeNull();
  });

  it('未預期 rejection 被接住並可見，不清除確認或回傳未處理拒絕', async () => {
    mocks.deleteSession.mockRejectedValue(new Error('synthetic-private-input'));
    const { result } = renderHook(() => useSessionActions());
    act(() => { result.current.handleDelete('a'); });
    await act(async () => { await expect(result.current.handleDelete('a')).resolves.toBeUndefined(); });
    expect(result.current.deleteConfirmId).toBe('a');
    expect(toast.error).toHaveBeenCalledTimes(1);
    const observedMessages = JSON.stringify(vi.mocked(toast.error).mock.calls);
    expect(observedMessages).not.toContain('synthetic-private-input');
  });

  it('home 仍拒絕，首次確認過期仍自動清除', () => {
    const { result } = renderHook(() => useSessionActions());
    act(() => { result.current.handleDelete('home'); });
    expect(mocks.deleteSession).not.toHaveBeenCalled();
    expect(toast.warning).toHaveBeenCalledTimes(1);
    act(() => { result.current.handleDelete('a'); vi.advanceTimersByTime(3000); });
    expect(result.current.deleteConfirmId).toBeNull();
  });

  it('真 Radix 選單於 pending 與失敗保留確認，Escape 仍可由使用者關閉', async () => {
    vi.useRealTimers();
    let finish!: (result: boolean) => void;
    mocks.deleteSession.mockReturnValue(new Promise<boolean>(resolve => { finish = resolve; }));
    function Item() {
      const actions = useSessionActions();
      return <SessionItem
        session={{ id: 'a', title: 'Synthetic session', messages: [], updatedAt: 1 }}
        isActive={false} editingSessionId={null} editTitle="" projects={[]}
        deleteConfirmId={actions.deleteConfirmId} onDelete={actions.handleDelete}
        onClearDeleteConfirm={actions.clearDeleteConfirm}
        onSwitch={vi.fn()} onStartEdit={vi.fn()} onSaveTitle={vi.fn()}
        onEditTitleChange={vi.fn()} onExport={vi.fn()} onSplitExport={vi.fn()}
        onSetHome={vi.fn()} onUnsetHome={vi.fn()} onSetProject={vi.fn()}
      />;
    }
    render(<Item />);
    const trigger = screen.getByRole('button', { expanded: false });
    fireEvent.keyDown(trigger, { key: 'Enter' });
    fireEvent.click(await screen.findByText('刪除對話'));
    fireEvent.click(screen.getByText('確認刪除？不可復原'));
    expect(mocks.deleteSession).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('menu')).toBeInTheDocument();
    expect(screen.getByText('確認刪除？不可復原')).toBeInTheDocument();
    await act(async () => { finish(false); });
    expect(screen.getByRole('menu')).toBeInTheDocument();
    expect(screen.getByText('確認刪除？不可復原')).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    fireEvent.keyDown(trigger, { key: 'Enter' });
    expect(await screen.findByText('刪除對話')).toBeInTheDocument();
  });
});
